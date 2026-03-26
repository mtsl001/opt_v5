# =============================================================================
# upstox.py — Production Market Data Collector
# -----------------------------------------------------------------------------
# Runs daily during NSE trading hours (09:15 → 15:30).
# Every ~1 minute it fetches a full market snapshot for ~4500 NSE F&O instruments
# (options, futures) + India VIX and uploads them to BigQuery.
#
# Architecture:
#   Phase 1 → Wait for trading day + 09:03 AM setup time
#   Phase 2 → Login (Playwright OAuth), download instruments, build lookup
#   Phase 3 → Pre-market wait, align to first :02 slot at 09:15:02
#   Phase 4 → 1-minute data collection loop until 15:30:45
#
# Key design decisions:
#   - instrument_keys_chunks: in-memory list-of-lists built ONCE at setup.
#     fetch_upstox_data() iterates these directly — never reads disk files.
#     This prevents data/used/ file deletion (by macOS or any external process)
#     from causing 0-row fetches mid-session.
#   - inst_lookup: dict built once from NSE.csv — avoids re-reading CSV every minute.
#   - BQ uploads run in a background daemon thread via bq_queue — main loop never blocks.
#   - sanitize_row() strips float NaN/Infinity — BQ rejects NaN as invalid JSON.
#   - spot_map: First pass in each loop captures all INDEX prices to ensure
#     consistent underlying_spot across all F&O instruments in that snapshot.
# =============================================================================

import time
import os
import json
import math
import logging
import threading
import queue
import calendar
import gzip
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed   # [FIX P2] parallel Greeks fetch
from datetime import datetime, timedelta, time as dtime
from urllib.parse import urlparse, parse_qs

import requests
import pandas as pd
import pyotp
from dotenv import load_dotenv
from google.cloud import bigquery
from playwright.sync_api import sync_playwright


# =============================================================================
# Logging Configuration
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)


# =============================================================================
# Custom Exceptions
# =============================================================================
class SessionExpiredException(Exception):
    """Raised on HTTP 401 — triggers force re-login in the outer loop."""
    pass


# =============================================================================
# Helpers
# =============================================================================
def notify(message):
    """Timestamped console print — used for operator-facing status messages."""
    timestamp = datetime.now().strftime('%H:%M:%S')
    print(f"[{timestamp}] ✔ {message}")


def _safe_depth(symbol_data: dict, side: str, level: int, field: str):
    """
    Safely extract symbol_data['depth'][side][level][field].
    Returns None on any missing key, out-of-range index, or null value.
    Used to flatten Level-1 bid/ask depth into scalar BQ columns.
    """
    try:
        return symbol_data['depth'][side][level][field] or None
    except (KeyError, IndexError, TypeError):
        return None


def sanitize_row(row: dict) -> dict:
    """
    Replace float NaN and Infinity with None before JSON serialization.
    BigQuery's JSON loader rejects NaN as it is not valid JSON.
    Called on every row before appending to rows / vix_rows.
    """
    clean = {}
    for k, v in row.items():
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            clean[k] = None
        else:
            clean[k] = v
    return clean


def save_jsonl(path: str, rows: list):
    """
    Write list of dicts as JSONL (newline-delimited JSON).
    BigQuery LoadJobConfig(source_format=NEWLINE_DELIMITED_JSON) requires this format.
    """
    with open(path, 'w') as f:
        for row in rows:
            f.write(json.dumps(row) + '\n')


def next_slot_at_second(target_second: int = 2) -> datetime:
    """
    Returns the next datetime that falls on :target_second of a minute.
    Used to align each fetch cycle to a consistent sub-second slot.

    Examples (target_second=2):
      now=09:21:00 → 09:21:02
      now=09:21:01 → 09:21:02
      now=09:21:02 → 09:22:02  (already at/past target — advance to next minute)
      now=09:21:45 → 09:22:02
    """
    now = datetime.now()
    if now.second < target_second:
        return now.replace(second=target_second, microsecond=0)
    else:
        return (now + timedelta(minutes=1)).replace(second=target_second, microsecond=0)


# =============================================================================
# Environment & Paths
# =============================================================================
load_dotenv()

# Google Cloud credentials — must be co-located with upstox.py or path provided in .env
gcp_json_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_PATH")
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), gcp_json_path
)
logging.info(f"BigQuery credentials set: {os.environ['GOOGLE_APPLICATION_CREDENTIALS']}")

# Folder paths
FOLDER_PATH         = os.path.join('data', 'used')       # debug-only disk writes
INSTRUMENTS_FOLDER  = 'instruments'
INSTRUMENTS_CSV     = os.path.join(INSTRUMENTS_FOLDER, "upstox_instruments.csv")
MASTER_DATA_PATH    = os.path.join('data', 'master_data')
SESSION_FILE        = "upstox_session.json"
HOLIDAYS_CACHE_FILE = "market_holidays.json"

# BigQuery — production tables from .env
TABLE_ID     = os.getenv("BQ_TABLE_ID")
VIX_TABLE_ID = os.getenv("BQ_VIX_TABLE_ID")

# BQ upload queue — main thread puts (table_id, rows), worker thread consumes
bq_queue = queue.Queue()


# =============================================================================
# BigQuery Background Worker
# =============================================================================
def bq_worker():
    """
    Long-running daemon thread for BigQuery uploads.
    Consumes (table_id, rows) tuples from bq_queue.
    Receives None as shutdown signal.

    Running uploads in background means the main fetch loop is never blocked
    waiting for a BQ job to complete (~5-10s per upload).
    """
    logging.info("[BIGQUERY] Initializing BigQuery client...")
    try:
        client = bigquery.Client()
        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            autodetect=True,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            # Allow new columns and nullable changes — handles schema evolution
            schema_update_options=[
                bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION,
                bigquery.SchemaUpdateOption.ALLOW_FIELD_RELAXATION
            ]
        )
        logging.info(f"[BIGQUERY] Client initialized. Production table: {TABLE_ID}")
    except Exception as e:
        logging.error(f"[BIGQUERY] Init failed: {e}. Check GOOGLE_APPLICATION_CREDENTIALS.")
        return

    logging.info("[BIGQUERY] Worker started, waiting for data...")

    while True:
        item = bq_queue.get()

        # None is the shutdown signal — drain and exit
        if item is None:
            logging.info("[BIGQUERY] Shutdown signal received. Exiting worker.")
            break

        table_id, rows = item
        try:
            logging.info(f"[BIGQUERY] Uploading {len(rows)} rows to {table_id}...")
            job = client.load_table_from_json(rows, table_id, job_config=job_config)
            job.result()   # blocks this thread only, not the main loop
            logging.info(f"[BIGQUERY] Upload complete — {len(rows)} rows inserted.")
        except Exception as e:
            logging.error(f"[BIGQUERY] Upload error to {table_id}: {e}")
            if hasattr(e, 'errors'):
                for err in e.errors:
                    logging.error(f"[BIGQUERY] Detail: {err}")
        finally:
            bq_queue.task_done()


# =============================================================================
# Market Holidays
# =============================================================================
def fetch_market_holidays(access_token):
    """
    Fetch NSE/NFO trading holidays from Upstox API.
    Returns list of date strings in YYYY-MM-DD format.
    Caches result to HOLIDAYS_CACHE_FILE for same-day reuse.
    """
    try:
        url = 'https://api.upstox.com/v2/market/holidays'
        headers = {
            'accept': 'application/json',
            'Api-Version': '2.0',
            'Authorization': f'Bearer {access_token}'
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get('status') == 'success':
            holidays = []
            for item in data.get('data', []):
                closed_exchanges = item.get('closed_exchanges', [])
                if 'NFO' in closed_exchanges or 'NSE' in closed_exchanges:
                    holidays.append(item['date'])

            # Cache to disk for same-day reuse across restarts
            with open(HOLIDAYS_CACHE_FILE, 'w') as f:
                json.dump({'holidays': holidays, 'fetched_at': datetime.now().isoformat()}, f)

            logging.info(f"[HOLIDAYS] Fetched {len(holidays)} holidays from API")
            return holidays
        else:
            logging.warning(f"[HOLIDAYS] Non-success API response: {data}")
            return []
    except Exception as e:
        logging.error(f"[HOLIDAYS] API fetch failed: {e}")
        return []


def get_market_holidays(access_token=None):
    """
    Returns trading holidays as list of YYYY-MM-DD strings.
    Priority: disk cache (same-day) → API (if token available) → hardcoded fallback.
    """
    # 1. Check disk cache
    if os.path.exists(HOLIDAYS_CACHE_FILE):
        try:
            with open(HOLIDAYS_CACHE_FILE, 'r') as f:
                cache_data = json.load(f)
            holidays   = cache_data.get('holidays', [])
            fetched_at = datetime.fromisoformat(cache_data.get('fetched_at'))
            if fetched_at.date() >= datetime.now().date():
                logging.info(f"[HOLIDAYS] Using cached holidays ({len(holidays)} dates)")
                return holidays
        except Exception as e:
            logging.warning(f"[HOLIDAYS] Cache read error: {e}")

    # 2. Fetch from Upstox API
    if access_token:
        holidays = fetch_market_holidays(access_token)
        if holidays:
            return holidays

    # 3. Hardcoded 2026 fallback — used only if API unreachable at startup
    logging.warning("[HOLIDAYS] Using hardcoded 2026 fallback holidays")
    return [
        "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
        "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
        "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24",
        "2026-12-25",
    ]


def is_trading_day(check_date=None, holidays=None):
    """
    Returns (is_trading: bool, reason: str).
    Checks: weekend → holiday list → trading day.
    """
    if check_date is None:
        check_date = datetime.now().date()
    if holidays is None:
        holidays = []

    date_str = check_date.strftime("%Y-%m-%d")
    day_name = check_date.strftime("%A")

    if check_date.weekday() == 5:
        return False, f"Saturday ({date_str}) — markets closed."
    if check_date.weekday() == 6:
        return False, f"Sunday ({date_str}) — markets closed."
    if date_str in holidays:
        return False, f"Trading holiday ({date_str}) — markets closed."

    return True, f"Trading day: {day_name}, {date_str}"


# =============================================================================
# Upstox Authentication (Playwright + OAuth)
# =============================================================================
def get_upstox_access_tokens(force_refresh=False):
    """
    Returns list of Upstox access tokens (one per configured account).
    Uses Playwright to automate the OAuth TOTP login flow for each account.
    Tokens are cached to SESSION_FILE and reused within the same calendar day.

    force_refresh=True: delete cache and re-login (used after SessionExpiredException).
    """
    logging.info("[SESSION] Checking for cached tokens...")

    # Clear cache if forced re-login requested
    if force_refresh and os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
        notify("Session cache cleared — performing fresh login")
        logging.info("[SESSION] Cache cleared for forced re-login.")

    # Use cache if tokens were obtained today
    if os.path.exists(SESSION_FILE):
        try:
            if os.path.getsize(SESSION_FILE) > 0:
                with open(SESSION_FILE, 'r') as f:
                    data = json.load(f)
                cached_date = datetime.fromtimestamp(data.get('timestamp', 0)).date()
                today       = datetime.now().date()
                if cached_date < today:
                    logging.info(f"[SESSION] Tokens from {cached_date} — stale, refreshing...")
                    notify(f"New trading day — refreshing tokens (cached from {cached_date})")
                    os.remove(SESSION_FILE)
                else:
                    logging.info("[SESSION] Using cached tokens from today")
                    notify("Using cached tokens from today")
                    return data['tokens']
            else:
                logging.warning("[SESSION] Cache file is empty. Deleting...")
                os.remove(SESSION_FILE)
        except Exception as e:
            logging.warning(f"[SESSION] Cache read error: {e}")
            if os.path.exists(SESSION_FILE): os.remove(SESSION_FILE)
    else:
        logging.info("[SESSION] No cache found — fresh login required.")
        notify("No cached tokens — starting browser login")

    # Load account credentials dynamically from .env
    input_sets = []
    for i in range(1, 10):   # Support up to 9 accounts
        api_key = os.getenv(f"UPSTOX_API_KEY_{i}")
        if not api_key:
            break

        # Strip trailing slash — Upstox is extremely sensitive to this mismatch
        rurl = os.getenv(f"UPSTOX_RURL_{i}").rstrip('/')

        from urllib.parse import quote
        encoded_rurl = quote(rurl, safe='')

        input_sets.append({
            "API_KEY":    api_key,
            "SECRET_KEY": os.getenv(f"UPSTOX_SECRET_KEY_{i}"),
            "RURL":       rurl,
            "TOTP_KEY":   os.getenv(f"UPSTOX_TOTP_KEY_{i}"),
            "MOBILE_NO":  os.getenv(f"UPSTOX_MOBILE_NO_{i}"),
            "PIN":        os.getenv(f"UPSTOX_PIN_{i}"),
            "AUTH_URL":   f"https://api-v2.upstox.com/login/authorization/dialog?response_type=code&client_id={api_key}&redirect_uri={encoded_rurl}"
        })

    if not input_sets:
        logging.error("[LOGIN] No account credentials found in .env!")
        raise ValueError("Missing UPSTOX_API_KEY_1 in environment")

    access_tokens = []
    logging.info(f"[LOGIN] Starting Playwright OAuth for {len(input_sets)} accounts...")
    notify(f"Starting browser login for {len(input_sets)} accounts...")

    with sync_playwright() as p:
        for index, inputs in enumerate(input_sets):
            logging.info(f"[LOGIN] Account {index + 1}/{len(input_sets)}...")
            notify(f"Logging in to account {index + 1}/{len(input_sets)}...")

            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = browser.new_context()
            page    = context.new_page()

            try:
                # Lenient request expectation — just wait for the RURL to appear in history
                with page.expect_request(lambda request: inputs['RURL'] in request.url and "code=" in request.url, timeout=60000) as request:
                    page.goto(inputs["AUTH_URL"])
                    # Enter Mobile Number
                    page.locator("#mobileNum").wait_for(state="visible", timeout=10000)
                    page.locator("#mobileNum").fill(inputs["MOBILE_NO"])
                    time.sleep(1)

                    # Click Get OTP
                    page.get_by_role("button", name="Get OTP").click()

                    # Wait for OTP field and fill it (TOTP bypass)
                    page.locator("#otpNum").wait_for(state="visible", timeout=20000)
                    page.locator("#otpNum").fill(pyotp.TOTP(inputs["TOTP_KEY"]).now())
                    time.sleep(1)

                    # Click Continue to PIN page
                    page.get_by_role("button", name="Continue").click()

                    # Wait for PIN field and fill it
                    page.get_by_label("Enter 6-digit PIN").wait_for(state="visible", timeout=20000)
                    page.get_by_label("Enter 6-digit PIN").fill(inputs["PIN"])

                    # Click Final Continue
                    page.get_by_role("button", name="Continue").click()
                    page.wait_for_load_state("networkidle")

                # Extract auth code from redirected URL
                code = parse_qs(urlparse(request.value.url).query)['code'][0]
                logging.info(f"[LOGIN] Account {index + 1} — auth code obtained")
                context.close()
                browser.close()

            except Exception as e:
                logging.error(f"[LOGIN] Account {index + 1} Playwright error: {e}")
                browser.close()
                raise

            # Exchange auth code for access token
            time.sleep(2)
            try:
                resp = requests.post(
                    'https://api-v2.upstox.com/login/authorization/token',
                    headers={
                        'accept': 'application/json',
                        'Api-Version': '2.0',
                        'Content-Type': 'application/x-www-form-urlencoded'
                    },
                    data={
                        'code':          code,
                        'client_id':     inputs["API_KEY"],
                        'client_secret': inputs["SECRET_KEY"],
                        'redirect_uri':  inputs["RURL"],
                        'grant_type':    'authorization_code'
                    }
                )
                resp.raise_for_status()
                access_tokens.append(resp.json()['access_token'])
                logging.info(f"[LOGIN] Account {index + 1} — token obtained")
                notify(f"Account {index + 1} authenticated successfully")
            except Exception as e:
                logging.error(f"[LOGIN] Account {index + 1} token exchange failed: {e}")
                raise

            time.sleep(2)

    # Cache tokens for same-day reuse
    with open(SESSION_FILE, 'w') as f:
        json.dump({'tokens': access_tokens, 'timestamp': datetime.now().timestamp()}, f)
    logging.info("[SESSION] Tokens cached successfully.")
    notify("All 3 access tokens obtained and cached")

    return access_tokens


# =============================================================================
# Instruments Download & Filtering
# =============================================================================
def download_and_filter_instruments():
    """
    Downloads NSE.csv.gz from Upstox, filters to F&O instruments for
    current + next month expiries, and builds all data structures needed
    for the day's fetch loop.
    """
    logging.info("[INSTRUMENTS] Starting download...")
    notify("Downloading instruments...")

    os.makedirs(INSTRUMENTS_FOLDER, exist_ok=True)
    os.makedirs(FOLDER_PATH, exist_ok=True)

    # Clean debug folder
    logging.info(f"[SETUP] Cleaning debug folder: {FOLDER_PATH}")
    deleted_count = 0
    if os.path.exists(FOLDER_PATH):
        for file_name in os.listdir(FOLDER_PATH):
            file_path = os.path.join(FOLDER_PATH, file_name)
            if os.path.isfile(file_path):
                os.remove(file_path)
                deleted_count += 1
    logging.info(f"[SETUP] Deleted {deleted_count} stale debug files.")

    # Download NSE.csv.gz and extract
    gz_url              = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.csv.gz"
    gz_save_path        = os.path.join(INSTRUMENTS_FOLDER, "NSE.csv.gz")
    extracted_file_path = os.path.join(INSTRUMENTS_FOLDER, "NSE.csv")

    success = False
    for attempt in range(2):
        try:
            logging.info(f"[INSTRUMENTS] Download attempt {attempt + 1}/2...")
            response = requests.get(gz_url, stream=True, timeout=30)
            response.raise_for_status()

            with open(gz_save_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            with gzip.open(gz_save_path, 'rb') as f_in:
                with open(extracted_file_path, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)

            logging.info(f"[INSTRUMENTS] Successfully downloaded and extracted → {extracted_file_path}")
            success = True
            break
        except Exception as e:
            logging.warning(f"[INSTRUMENTS] Download attempt {attempt + 1} failed: {e}")
            if attempt == 0:
                time.sleep(3)

    if not success:
        logging.error("[INSTRUMENTS] All download attempts failed. Checking for local fallback...")
        if os.path.exists(extracted_file_path):
            mtime = os.path.getmtime(extracted_file_path)
            file_age_hours = (time.time() - mtime) / 3600
            if file_age_hours < 24:
                logging.info(f"[INSTRUMENTS] Using local fallback file (Age: {file_age_hours:.1f} hours)")
                notify("Using local instrument cache (download failed)")
            else:
                logging.error(f"[INSTRUMENTS] Local file too old ({file_age_hours:.1f}h). Cannot continue.")
                raise Exception("Instrument download failed and no fresh local cache found")
        else:
            logging.error("[INSTRUMENTS] No local fallback file found. Cannot continue.")
            raise Exception("Instrument download failed and no local cache found")

    df = pd.read_csv(extracted_file_path, low_memory=False)
    logging.info(f"[INSTRUMENTS] Loaded {len(df)} total instruments")

    # Capture India VIX and major Indices — these are NSE_INDEX exchange
    indices_to_capture = [
        'NSE_INDEX|India VIX',
        'NSE_INDEX|Nifty 50',
        'NSE_INDEX|Nifty Bank',
        'NSE_INDEX|Nifty Fin Service',
        'NSE_INDEX|NIFTY MID SELECT',
        'NSE_INDEX|Nifty Next 50',
        'NSE_INDEX|Nifty 500',
        'NSE_INDEX|Nifty Auto',
        'NSE_INDEX|Nifty Consumption',
        'NSE_INDEX|Nifty CPSE',
        'NSE_INDEX|Nifty Energy',
        'NSE_INDEX|Nifty EV',
        'NSE_INDEX|Nifty FMCG',
        'NSE_INDEX|NIFTY HEALTHCARE',
        'NSE_INDEX|Nifty Infra',
        'NSE_INDEX|Nifty IT',
        'NSE_INDEX|Nifty Media',
        'NSE_INDEX|Nifty Metal',
        'NSE_INDEX|NIFTY MICROCAP250',
        'NSE_INDEX|NIFTY MIDCAP 100',
        'NSE_INDEX|Nifty Mobility',
        'NSE_INDEX|Nifty New Consump',
        'NSE_INDEX|NIFTY OIL AND GAS',
        'NSE_INDEX|Nifty Pharma',
        'NSE_INDEX|Nifty PSU Bank',
        'NSE_INDEX|Nifty Realty',
        'NSE_INDEX|Nifty Sml250 Q50',
        'NSE_INDEX|NIFTY SMLCAP 100',
        'NSE_INDEX|Nifty500 Qlty50',
        'NSE_INDEX|Nifty500Momentm50'
    ]
    vix_df = df[df['instrument_key'].isin(indices_to_capture)].copy()

    # Filter: NSE_FO exchange only
    fo_df = df[df['exchange'] == 'NSE_FO'].copy()
    logging.info(f"[INSTRUMENTS] NSE_FO filter → {len(fo_df)} instruments")

    # Filter: relevant instrument types only
    allowed_types = ['OPTIDX', 'FUTSTK', 'FUTIDX']
    fo_df = fo_df[fo_df['instrument_type'].isin(allowed_types)].copy()
    logging.info(f"[INSTRUMENTS] Type filter (OPTIDX/FUTSTK/FUTIDX) → {len(fo_df)} instruments")

    # Filter: current month + next month expiries only
    today = datetime.today()
    try:
        first_day_next = datetime(today.year, today.month + 1, 1)
    except ValueError:
        first_day_next = datetime(today.year + 1, 1, 1)

    last_day       = calendar.monthrange(first_day_next.year, first_day_next.month)[1]
    next_month_end = first_day_next.replace(day=last_day)

    fo_df['expiry_dt'] = pd.to_datetime(fo_df['expiry'])

    # [FIX 4] Use <= to include instruments expiring ON the last day of next month.
    # The original < incorrectly dropped monthly expiry contracts on that date.
    filtered_fo = fo_df[fo_df['expiry_dt'] <= next_month_end].copy()
    logging.info(f"[INSTRUMENTS] Expiry filter → {len(filtered_fo)} instruments (cur + next month)")

    # Append India VIX and Indices back after filtering
    if not vix_df.empty:
        filtered = pd.concat([filtered_fo, vix_df], ignore_index=True)
        logging.info("[INSTRUMENTS] India VIX and Indices appended.")
    else:
        filtered = filtered_fo

    # [FIX P1] Build inst_lookup using vectorized .to_dict('index') instead of iterrows().
    # iterrows() on 4500+ rows is 10-100x slower than this single vectorized call.
    # This also prevents storing metadata for irrelevant OPTSTK instruments.
    inst_lookup = (
        filtered[
            ['instrument_key', 'instrument_type', 'name',
             'tradingsymbol', 'expiry', 'strike', 'option_type']
        ]
        .set_index('instrument_key')
        .to_dict('index')
    )
    logging.info(f"[LOOKUP] Built inst_lookup with {len(inst_lookup)} relevant instruments")

    instrument_keys = filtered['instrument_key'].tolist()

    # -----------------------------------------------------------------
    # Build in-memory chunks — THE KEY FIX
    # fetch_upstox_data() iterates instrument_keys_chunks directly.
    # Disk files (Part01.txt etc.) are written alongside for debug only.
    # If macOS or any external process deletes data/used/ files,
    # the fetch loop is completely unaffected.
    # -----------------------------------------------------------------
    keys_per_file          = 485
    num_parts              = 0
    instrument_keys_chunks = []   # primary: in-memory list of lists

    for i in range(0, len(instrument_keys), keys_per_file):
        chunk = instrument_keys[i:i + keys_per_file]
        instrument_keys_chunks.append(chunk)   # stored in RAM

        # Debug-only disk write — never read back by fetch loop
        part_file = os.path.join(FOLDER_PATH, f"Part{i//keys_per_file + 1:02d}.txt")
        with open(part_file, 'w') as f:
            for key in chunk:
                f.write(f"{key}\n")
        num_parts += 1

    logging.info(f"[INSTRUMENTS] {len(instrument_keys)} instruments → {num_parts} chunks (in-memory)")
    notify(f"Instruments ready: {num_parts} chunks, {len(instrument_keys)} keys")

    # Save filtered CSV for external reference / auditing
    filtered.to_csv(INSTRUMENTS_CSV, index=False)
    logging.info(f"[INSTRUMENTS] Filtered CSV saved → {INSTRUMENTS_CSV}")

    # 4 return values — instrument_keys_chunks is new vs old code
    return instrument_keys, filtered, inst_lookup, instrument_keys_chunks


# =============================================================================
# Greeks Data Fetching (Option Chain API)
# =============================================================================
def fetch_greeks_data(access_tokens, instruments_df):
    """
    Fetches Greeks (delta, gamma, theta, vega, IV) and option chain market data
    from Upstox Option Chain API for all OPTIDX instruments.

    Groups options by (underlying_key, expiry_date) — one API call per group.

    [FIX P2] Requests are now dispatched in parallel using ThreadPoolExecutor
    with max_workers=len(access_tokens). Each worker is assigned a token via
    round-robin, cutting total fetch time from ~25s to ~8-10s.

    Returns:
        greeks_map (dict): instrument_key → {delta, gamma, theta, vega, iv,
                                              oc_ltp, oc_volume, oc_oi,
                                              oc_close_price, oc_pcr,
                                              oc_underlying_spot_price}
    """
    logging.info("[GREEKS] Starting Option Chain Greeks fetch...")

    # Maps underlying name (from instruments CSV) to its index instrument_key
    underlying_map = {
        'NIFTY':      'NSE_INDEX|Nifty 50',
        'BANKNIFTY':  'NSE_INDEX|Nifty Bank',
        'FINNIFTY':   'NSE_INDEX|Nifty Fin Service',
        'MIDCPNIFTY': 'NSE_INDEX|NIFTY MID SELECT',
        'NIFTYNXT50': 'NSE_INDEX|Nifty Next 50',
    }

    # [FIX 3] Only OPTIDX is present in instruments_df — OPTSTK is excluded by
    # download_and_filter_instruments (not in allowed_types). The old check for
    # ['OPTIDX', 'OPTSTK'] was dead code. Filter is now correctly OPTIDX-only.
    #
    # [FIX P1] Build option_groups without iterrows() — use vectorized pandas
    # filter + groupby to identify (underlying_key, expiry) pairs.
    fo_instruments = instruments_df[instruments_df['instrument_type'] == 'OPTIDX'].copy()
    fo_instruments['underlying_key'] = fo_instruments['name'].map(underlying_map)
    fo_instruments = fo_instruments[
        fo_instruments['underlying_key'].notna() &
        fo_instruments['expiry'].notna() &
        (fo_instruments['expiry'] != '')
    ]

    # Build option_groups: {(underlying_key, expiry): list_of_row_dicts}
    option_groups = {
        (uk, exp): group.to_dict('records')
        for (uk, exp), group in fo_instruments.groupby(['underlying_key', 'expiry'])
    }

    logging.info(f"[GREEKS] {len(option_groups)} underlying+expiry combinations to fetch")

    # -------------------------------------------------------------------------
    # Inner worker: fetches one (underlying_key, expiry_date) group.
    # Called in parallel by ThreadPoolExecutor below.
    # Raises SessionExpiredException on 401 — propagated through the future.
    # -------------------------------------------------------------------------
    def fetch_one_group(underlying_key, expiry_date, token):
        headers = {
            'accept':        'application/json',
            'Api-Version':   '2.0',
            'Authorization': f'Bearer {token}'
        }

        resp = requests.get(
            'https://api.upstox.com/v2/option/chain',
            params={'instrument_key': underlying_key, 'expiry_date': expiry_date},
            headers=headers,
            timeout=15
        )

        # Handle session expiration in Greeks fetch —
        # If 401 occurs, we raise SessionExpiredException to trigger a re-login
        if resp.status_code == 401:
            raise SessionExpiredException(f"Greeks API unauthorized (401) for {underlying_key}")

        resp.raise_for_status()
        data = resp.json()

        result = {}

        if data.get('status') == 'success':
            chain_data = data.get('data', [])

            # PCR and spot price are chain-level — same value for all strikes in this chain
            pcr                   = chain_data[0].get('pcr')                   if chain_data else None
            underlying_spot_price = chain_data[0].get('underlying_spot_price') if chain_data else None

            for chain_item in chain_data:
                # Process CE (call) side
                call_opts = chain_item.get('call_options', {})
                if call_opts and 'market_data' in call_opts:
                    call_key = call_opts.get('instrument_key', '').replace(':', '|')
                    if call_key:
                        g = call_opts.get('option_greeks', {})
                        m = call_opts.get('market_data', {})
                        result[call_key] = {
                            'delta': g.get('delta'), 'gamma': g.get('gamma'),
                            'theta': g.get('theta'), 'vega':  g.get('vega'),
                            'iv':    g.get('iv'),
                            'oc_ltp':                   m.get('ltp'),
                            'oc_volume':                m.get('volume'),
                            'oc_oi':                    m.get('oi'),
                            'oc_close_price':           m.get('close_price'),
                            'oc_pcr':                   pcr,
                            'oc_underlying_spot_price': underlying_spot_price
                        }

                # Process PE (put) side
                put_opts = chain_item.get('put_options', {})
                if put_opts and 'market_data' in put_opts:
                    put_key = put_opts.get('instrument_key', '').replace(':', '|')
                    if put_key:
                        g = put_opts.get('option_greeks', {})
                        m = put_opts.get('market_data', {})
                        result[put_key] = {
                            'delta': g.get('delta'), 'gamma': g.get('gamma'),
                            'theta': g.get('theta'), 'vega':  g.get('vega'),
                            'iv':    g.get('iv'),
                            'oc_ltp':                   m.get('ltp'),
                            'oc_volume':                m.get('volume'),
                            'oc_oi':                    m.get('oi'),
                            'oc_close_price':           m.get('close_price'),
                            'oc_pcr':                   pcr,
                            'oc_underlying_spot_price': underlying_spot_price
                        }

        return result

    # -------------------------------------------------------------------------
    # [FIX P2] Parallel dispatch — one thread per token, round-robin assignment.
    # as_completed() collects results as each future finishes rather than in
    # submission order, maximising throughput across all 3 tokens.
    # -------------------------------------------------------------------------
    greeks_map  = {}
    group_items = list(option_groups.items())

    with ThreadPoolExecutor(max_workers=len(access_tokens)) as executor:
        # Submit all groups at once; assign token by round-robin index
        futures = {
            executor.submit(
                fetch_one_group, underlying_key, expiry_date,
                access_tokens[idx % len(access_tokens)]
            ): (underlying_key, expiry_date)
            for idx, ((underlying_key, expiry_date), _) in enumerate(group_items)
        }

        for future in as_completed(futures):
            underlying_key, expiry_date = futures[future]
            try:
                result        = future.result()
                initial_count = len(greeks_map)
                greeks_map.update(result)
                logging.info(
                    f"[GREEKS] {underlying_key} {expiry_date}: "
                    f"+{len(greeks_map) - initial_count} options (total: {len(greeks_map)})"
                )
            except SessionExpiredException:
                raise   # Propagate to trigger re-login in the outer loop
            except Exception as e:
                logging.warning(f"[GREEKS] Failed for {underlying_key} {expiry_date}: {e}")
                continue

    logging.info(f"[GREEKS] Done — Greeks fetched for {len(greeks_map)} instruments")
    return greeks_map


# =============================================================================
# Market Data Fetching (Market Quote API)
# =============================================================================
def fetch_upstox_data(access_tokens, instruments_df, inst_lookup, instrument_keys_chunks):
    """
    Fetches live market quotes for all instruments and merges with Greeks.

    This function implements a "Two-Pass" logic to ensure spot price consistency:
      Pass 1: Capture all INDEX prices into a spot_map.
      Pass 2: Enrich F&O instruments using the metadata and the synchronized spot_map.

    [FIX P3] Market Quote fetching is now parallelized using ThreadPoolExecutor.
    Each chunk is dispatched to a worker with its own local headers/token,
    reducing quote fetch time from ~10s to ~3s.

    Parameters:
        access_tokens          (list)  — 3 Upstox tokens, rotated across chunks
        instruments_df         (DataFrame) — used by fetch_greeks_data()
        inst_lookup            (dict)  — instrument_key → metadata, for row enrichment
        instrument_keys_chunks (list of lists) — IN-MEMORY chunks, never reads disk

    Returns:
        rows     (list of dict) — main instrument rows for TABLE_ID
        vix_rows (list of dict) — India VIX row for VIX_TABLE_ID
    """
    # Capture snapshot timestamp BEFORE any API calls — Greeks fetch takes ~8s
    record_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Fetch Greeks first — Option Chain API (parallelized, ~8-10s)
    greeks_map = fetch_greeks_data(access_tokens, instruments_df)

    url = 'https://api-v2.upstox.com/market-quote/quotes'

    logging.info(f"[DATA] Fetching {len(instrument_keys_chunks)} chunks in parallel...")

    # -------------------------------------------------------------------------
    # Inner worker: fetches one chunk of ~485 symbols with retry + token rotation.
    # Each worker gets its own headers dict — no shared mutable state between threads.
    # -------------------------------------------------------------------------
    def fetch_one_chunk(chunk, token_idx):
        current_token_idx = token_idx % len(access_tokens)
        chunk_headers = {
            'accept':        'application/json',
            'Api-Version':   '2.0',
            'Authorization': f'Bearer {access_tokens[current_token_idx]}'
        }
        params      = {'symbol': ','.join(chunk)}
        retry_count = 0
        max_retries = 3

        while retry_count < max_retries:
            try:
                resp = requests.get(url, params=params, headers=chunk_headers, timeout=10)

                if resp.status_code == 401:
                    raise SessionExpiredException("Access token expired (HTTP 401)")

                resp.raise_for_status()
                response_data_single = resp.json()

                if response_data_single:
                    return response_data_single.get('data', {})
                else:
                    logging.warning(f"[DATA] Empty response for chunk (token {current_token_idx})")
                    return {}

            except SessionExpiredException:
                raise   # do not retry — propagate immediately to trigger re-login

            except requests.ConnectionError as ce:
                # Rotate to next token on connection error
                logging.warning(f"[DATA] Connection error chunk {token_idx}: {ce}. Rotating token...")
                retry_count       += 1
                time.sleep(2)
                current_token_idx  = (current_token_idx + 1) % len(access_tokens)
                chunk_headers['Authorization'] = f'Bearer {access_tokens[current_token_idx]}'

            except (requests.Timeout, requests.HTTPError) as e:
                logging.warning(f"[DATA] Error chunk {token_idx}: {e}. Retry {retry_count + 1}/{max_retries}...")
                retry_count += 1
                time.sleep(2)

            except Exception as ex:
                logging.error(f"[DATA] Unexpected error chunk {token_idx}: {ex}")
                retry_count += 1
                time.sleep(2)

        logging.error(f"[DATA] Chunk {token_idx} failed after {max_retries} retries — skipped")
        return {}

    # -------------------------------------------------------------------------
    # [FIX P3] Parallel dispatch — one thread per token, round-robin assignment.
    # Cutting total quote fetch time from ~10s sequential to ~3-4s.
    # -------------------------------------------------------------------------
    combined_data = {}

    with ThreadPoolExecutor(max_workers=len(access_tokens)) as executor:
        # Submit all chunks at once; assign token by round-robin index
        futures = {
            executor.submit(fetch_one_chunk, chunk, idx): idx
            for idx, chunk in enumerate(instrument_keys_chunks)
        }

        for future in as_completed(futures):
            chunk_idx = futures[future]
            try:
                chunk_result = future.result()
                combined_data.update(chunk_result)
                logging.info(f"[DATA] Chunk {chunk_idx + 1} complete — {len(chunk_result)} symbols")
            except SessionExpiredException:
                raise   # Propagate to trigger re-login in the outer loop
            except Exception as e:
                logging.error(f"[DATA] Chunk {chunk_idx + 1} error: {e} — skipped")
                continue

    logging.info(f"[DATA] All chunks done — {len(combined_data)} total symbols received")

    # -------------------------------------------------------------------------
    # PASS 1: Build spot_map from INDEX instruments for synchronized mapping
    # -------------------------------------------------------------------------
    # Bridge between short names (from FO instruments) and full names (from INDEX)
    name_bridge = {
        'Nifty 50':           'NIFTY',
        'Nifty Bank':         'BANKNIFTY',
        'Nifty Fin Service':  'FINNIFTY',
        'NIFTY MID SELECT':   'MIDCPNIFTY',
        'Nifty Next 50':      'NIFTYNXT50'
    }

    spot_map = {}
    for symbol, symbol_data in combined_data.items():
        if symbol_data is None:
            continue

        # Normalize key (Upstox API often uses : instead of |)
        normalized_symbol = symbol.replace(':', '|')
        inst_details      = inst_lookup.get(normalized_symbol, {})

        if inst_details.get('instrument_type') == 'INDEX':
            # Use last_price if available, else fallback to close
            price          = symbol_data.get('last_price') or (symbol_data.get('ohlc') or {}).get('close')
            full_name      = inst_details.get('name', '')
            trading_symbol = inst_details.get('tradingsymbol', '')

            # Map full name, trading symbol, and short name (if bridged)
            if full_name:      spot_map[full_name]      = price
            if trading_symbol: spot_map[trading_symbol] = price

            if full_name in name_bridge:
                spot_map[name_bridge[full_name]] = price

    # -------------------------------------------------------------------------
    # PASS 2: Transform raw API response → flat dicts ready for BigQuery
    # -------------------------------------------------------------------------
    rows     = []
    vix_rows = []

    for symbol, symbol_data in combined_data.items():
        if symbol_data is None:
            continue

        # instrument_token from Market Quote API = instrument_key in master CSV
        # Normalize token to ensure lookup succeeds
        instrument_token = symbol_data.get('instrument_token', '').replace(':', '|')
        inst_details     = inst_lookup.get(instrument_token, {})
        ohlc             = symbol_data.get('ohlc') or {}

        # Guard against None / empty string / 0 strike before float conversion
        raw_strike   = inst_details.get('strike')
        strike_price = float(raw_strike) if raw_strike not in (None, '', 0) else None

        # Pre-initialise Greeks/Data to None
        row_delta           = None
        row_gamma           = None
        row_theta           = None
        row_vega            = None
        row_iv              = None
        row_ltp             = None
        row_volume          = None
        row_oi              = None
        row_close_price     = None
        row_pcr             = None
        row_underlying_spot = None

        instrument_type = inst_details.get('instrument_type', '')

        if instrument_type in ('FUTSTK', 'FUTIDX'):
            # Read directly from Market Quote symbol_data — no Greeks mapping needed
            row_ltp             = symbol_data.get('last_price')
            row_volume          = symbol_data.get('volume')
            row_oi              = symbol_data.get('oi')
            row_close_price     = ohlc.get('close')

            # Map spot price from our first-pass spot_map for consistency
            row_underlying_spot = spot_map.get(inst_details.get('name'))

        elif instrument_type == 'INDEX':
            # [FIX PX] Ensure INDEX rows have ltp populated for consistency
            row_ltp = symbol_data.get('last_price')

        else:
            # Options (OPTIDX) — use Option Chain greeks_map
            greeks_data = greeks_map.get(instrument_token, {})
            if greeks_data:
                row_delta           = greeks_data.get('delta')
                row_gamma           = greeks_data.get('gamma')
                row_theta           = greeks_data.get('theta')
                row_vega            = greeks_data.get('vega')
                row_iv              = greeks_data.get('iv')
                row_ltp             = greeks_data.get('oc_ltp')
                row_volume          = greeks_data.get('oc_volume')
                row_oi              = greeks_data.get('oc_oi')
                row_close_price     = greeks_data.get('oc_close_price')
                row_pcr             = greeks_data.get('oc_pcr')
                row_underlying_spot = greeks_data.get('oc_underlying_spot_price')

        row = {
            # --- Instrument identification ---
            'instrument_key':  instrument_token,
            'instrument_type': instrument_type,
            'underlying':      inst_details.get('name', ''),
            'expiry_date':     inst_details.get('expiry', ''),
            'strike_price':    strike_price,
            'option_type':     inst_details.get('option_type', ''),

            # --- OHLC price data ---
            'open':  ohlc.get('open'),
            'high':  ohlc.get('high'),
            'low':   ohlc.get('low'),
            'close': ohlc.get('close'),

            # --- Market depth totals ---
            'total_buy_qty':  int(symbol_data.get('total_buy_quantity')  or 0),
            'total_sell_qty': int(symbol_data.get('total_sell_quantity') or 0),

            # --- Market depth — all 5 levels, both sides (buy/sell) ---
            **{
                f'depth_bid{lvl+1}_{field}': _safe_depth(symbol_data, 'buy', lvl, field_key)
                for lvl in range(5)
                for field, field_key in [('qty', 'quantity'), ('price', 'price'), ('orders', 'orders')]
            },
            **{
                f'depth_ask{lvl+1}_{field}': _safe_depth(symbol_data, 'sell', lvl, field_key)
                for lvl in range(5)
                for field, field_key in [('qty', 'quantity'), ('price', 'price'), ('orders', 'orders')]
            },

            # --- Timestamps ---
            'last_trade_time': symbol_data.get('last_trade_time'),
            'record_time':     record_timestamp,

            # --- Greeks & Option Chain data ---
            'delta':           row_delta,
            'gamma':           row_gamma,
            'theta':           row_theta,
            'vega':            row_vega,
            'iv':              row_iv,
            'ltp':             row_ltp,
            'volume':          row_volume,
            'oi':              row_oi,
            'close_price':     row_close_price,
            'pcr':             row_pcr,
            'underlying_spot': row_underlying_spot,
        }

        # Route INDEX instruments to their own table
        if instrument_type == 'INDEX':
            vix_row = {
                "instrument_key":  instrument_token,
                "instrument_type": "INDEX",
                "underlying":      inst_details.get('name', ''),
                "option_type":     "INDEX",
                "open":            ohlc.get('open'),
                "high":            ohlc.get('high'),
                "low":             ohlc.get('low'),
                "close":           ohlc.get('close'),
                "ltp":             row_ltp,
                "last_trade_time": symbol_data.get('last_trade_time'),
                "record_time":     record_timestamp
            }
            vix_rows.append(sanitize_row(vix_row))
        else:
            rows.append(sanitize_row(row))

    logging.info(f"[DATA] Transformed {len(rows)} main rows and {len(vix_rows)} VIX rows")
    return rows, vix_rows


# =============================================================================
# Main Execution Loop
# =============================================================================
if __name__ == "__main__":

    # Start BigQuery background worker daemon — runs for the entire process lifetime
    threading.Thread(target=bq_worker, daemon=True).start()
    notify("BigQuery background worker started")

    # =========================================================================
    # Outer day loop — restarts every trading day automatically
    # =========================================================================
    while True:

        # =====================================================================
        # PHASE 1 — Wait for a valid trading day + 09:03 AM setup window
        # =====================================================================
        notify("Waiting for next trading session...")
        market_holidays = get_market_holidays()   # from cache or fallback

        while True:
            now   = datetime.now()
            today = now.date()

            is_trading, reason = is_trading_day(today, holidays=market_holidays)

            if is_trading:
                setup_time   = dtime(9, 3, 0)
                market_close = dtime(15, 30, 45)

                if now.time() >= market_close:
                    # Already past today's close — wait for next day
                    logging.info("[WAIT] Market closed for today. Waiting for next trading day...")
                    notify("Market closed — waiting for next trading day...")

                elif now.time() >= setup_time:
                    # Within trading window — proceed to login
                    logging.info(f"[TRADING DAY] {reason} — starting setup...")
                    notify(f"✅ {reason}")
                    break

                else:
                    # Too early — wait until 09:03
                    wait_sec = (datetime.combine(today, setup_time) - now).total_seconds()
                    logging.info(f"[WAIT] Trading day, but too early. {wait_sec/60:.1f} min until 09:03...")
                    notify(f"Trading day! Waiting {wait_sec/60:.1f} min until setup at 09:03...")
            else:
                logging.info(f"[WAIT] {reason}")
                notify(f"⛔ {reason}")

            # Re-check every 3 minutes
            logging.info("[WAIT] Checking again in 3 minutes...")
            time.sleep(180)

        # =====================================================================
        # PHASE 2 — Login, instruments download, setup
        # Wrapped in inner while loop to handle SessionExpiredException re-login
        # =====================================================================
        force_relogin = False

        while True:
            try:
                # --- Login ---
                access_tokens = get_upstox_access_tokens(force_refresh=force_relogin)
                force_relogin = False
                notify("Access tokens obtained")

                # Refresh holidays from API now that we have a live token
                market_holidays = get_market_holidays(access_token=access_tokens[0])
                notify(f"Market holidays refreshed ({len(market_holidays)} dates)")

                # --- Download instruments & build lookup ---
                # Returns 4 values — instrument_keys_chunks is the in-memory fix
                instrument_keys, instruments_df, inst_lookup, instrument_keys_chunks = \
                    download_and_filter_instruments()
                notify(f"Setup complete: {len(instrument_keys)} instruments, {len(instrument_keys_chunks)} chunks")

                # --- Verification fetch (pre-market health check) ---
                logging.info("[SETUP] Running verification fetch...")
                notify("Verifying setup with pre-market fetch...")
                try:
                    test_rows, test_vix_rows = fetch_upstox_data(
                        access_tokens, instruments_df, inst_lookup, instrument_keys_chunks
                    )
                    if test_rows or test_vix_rows:
                        logging.info(f"[SETUP] Verification OK — {len(test_rows)} rows, {len(test_vix_rows)} VIX")
                        notify(f"✅ Verification OK! ({len(test_rows)} rows, {len(test_vix_rows)} VIX)")
                    else:
                        logging.warning("[SETUP] Verification returned no data.")
                        notify("⚠️ Verification: no data returned — check market status")
                except SessionExpiredException:
                    raise   # propagate to outer except for re-login
                except Exception as e:
                    logging.error(f"[SETUP] Verification error: {e}")
                    notify(f"⚠️ Verification error: {e} — continuing anyway")

                # =============================================================
                # PHASE 3 — Pre-market alignment
                # =============================================================

                # Step 1: wait until 09:12 for scheduled session health check
                now        = datetime.now()
                check_time = datetime.combine(now.date(), dtime(9, 12, 0))
                if now < check_time:
                    wait_sec = (check_time - now).total_seconds()
                    logging.info(f"[WAIT] {wait_sec:.0f}s until 09:12 session check...")
                    notify(f"Waiting {wait_sec/60:.1f} min until 09:12 check...")
                    time.sleep(wait_sec)
                logging.info("[SESSION] 09:12 AM session check passed.")
                notify("✅ 09:12 session check passed")

                # Step 2: align to first :02 slot at/after 09:15
                now          = datetime.now()
                market_start = datetime.combine(now.date(), dtime(9, 15, 0))

                if now < market_start:
                    # Before 09:15 — target exactly 09:15:02
                    next_run = market_start + timedelta(seconds=2)
                else:
                    # Already past 09:15 (e.g. late restart) — next :02 slot
                    next_run = next_slot_at_second(2)

                wait_sec = (next_run - datetime.now()).total_seconds()
                if wait_sec > 0:
                    logging.info(f"[WAIT] First fetch scheduled at {next_run.strftime('%H:%M:%S')}...")
                    notify(f"First fetch at {next_run.strftime('%H:%M:%S')}")
                    time.sleep(wait_sec)

                # =============================================================
                # PHASE 4 — Data Collection Loop (09:15:02 → 15:30:45)
                # Fetches a full snapshot every ~1 minute, aligned to :02 seconds.
                # =============================================================
                while True:

                    # --- Market close check ---
                    if datetime.now().time() >= dtime(15, 30, 45):
                        logging.info("[MARKET] Closed for the day.")
                        notify("📊 Market closed — today's session complete.")
                        break

                    loop_start = datetime.now()
                    logging.info(f"[DATA] Snapshot at {loop_start.strftime('%H:%M:%S')}")
                    notify("Fetching data snapshot...")

                    # Fetch quotes + Greeks using in-memory chunks (never reads disk)
                    # This is the core execution loop that triggers every minute.
                    rows, vix_rows = fetch_upstox_data(
                        access_tokens, instruments_df, inst_lookup, instrument_keys_chunks
                    )
                    logging.info(f"[DATA] {len(rows)} main rows, {len(vix_rows)} VIX rows")
                    notify(f"Fetched {len(rows)} rows, {len(vix_rows)} VIX")

                    if rows or vix_rows:
                        # Save JSONL backup to disk for local audit/redundancy
                        ts = datetime.now().strftime('%d%m%y%H%M')
                        os.makedirs(MASTER_DATA_PATH, exist_ok=True)
                        jsonl_path = f"{MASTER_DATA_PATH}/{ts}.jsonl"
                        save_jsonl(jsonl_path, rows + vix_rows)
                        logging.info(f"[FILE] Saved → {jsonl_path}")
                        notify(f"JSONL saved: {ts}.jsonl")

                        # Queue for async BQ upload — main thread puts, daemon worker consumes.
                        # This ensures the main fetch loop is never delayed by network/BQ latency.
                        if rows:
                            logging.info(f"[BIGQUERY] Queueing {len(rows)} rows → {TABLE_ID}")
                            bq_queue.put((TABLE_ID, rows))
                        if vix_rows:
                            logging.info(f"[BIGQUERY] Queueing {len(vix_rows)} VIX rows → {VIX_TABLE_ID}")
                            bq_queue.put((VIX_TABLE_ID, vix_rows))
                        notify("Queued for BigQuery upload")
                    else:
                        logging.warning("[DATA] No rows fetched — skipping this cycle.")
                        notify("⚠️ WARNING: No rows fetched this cycle")

                    # Wait until next :02 slot (fresh now() for accurate calculation)
                    next_slot = next_slot_at_second(2)
                    wait      = (next_slot - datetime.now()).total_seconds()
                    if wait > 0:
                        logging.info(f"[WAIT] {wait:.1f}s until {next_slot.strftime('%H:%M:%S')}...")
                        time.sleep(wait)

                # Phase 4 loop ended (market closed) — break inner while to restart day loop
                break

            except SessionExpiredException as e:
                # Session expired mid-session — force re-login and retry Phase 2
                logging.error(f"[SESSION] Expired: {e} — forcing re-login...")
                notify("⚠️ SESSION EXPIRED — re-logging in...")
                force_relogin = True
                time.sleep(2)   # brief pause before re-login attempt

            except Exception as e:
                # Unexpected crash — log and restart Phase 2 after 10s
                logging.error(f"[CRASH] Unexpected error: {e} — restarting in 10s...")
                notify(f"⚠️ ERROR: {e} — restarting in 10s...")
                time.sleep(10)
