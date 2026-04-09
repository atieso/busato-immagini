import os
import re
import time
import json
import ftplib
import hashlib
import logging
import mimetypes
from io import BytesIO
from typing import Dict, List, Optional, Tuple

import requests
from pydantic import BaseModel

try:
    from fastapi import FastAPI, HTTPException
except Exception:
    FastAPI = None

    class HTTPException(Exception):
        def __init__(self, status_code: int = 500, detail=None):
            self.status_code = status_code
            self.detail = detail
            super().__init__(str(detail))


# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s | %(message)s",
    force=True,
)
log = logging.getLogger("shopify_ftp_sync")


# =========================
# Config
# =========================
SHOPIFY_SHOP = os.getenv("SHOPIFY_SHOP", "")
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-01")
SHOPIFY_FILE_READY_TIMEOUT = int(os.getenv("SHOPIFY_FILE_READY_TIMEOUT", "120"))

FTP_HOST = os.getenv("FTP_HOST", "")
FTP_USER = os.getenv("FTP_USER", "")
FTP_PASS = os.getenv("FTP_PASS", "")
FTP_BASE_DIR = os.getenv("FTP_BASE_DIR", "/")
FTP_PASSIVE = os.getenv("FTP_PASSIVE", "true").lower() == "true"
FTP_TIMEOUT = int(os.getenv("FTP_TIMEOUT", "30"))
FTP_SPLIT_DIRS = [x.strip() for x in os.getenv("FTP_SPLIT_DIRS", "").split(",") if x.strip()]

STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
FILENAME_RE = re.compile(r"^(?P<sku>.+?)(?:_(?P<index>\d+))?$", re.IGNORECASE)
TEST_MAX_FILES = int(os.getenv("TEST_MAX_FILES", "0"))


# =========================
# FastAPI app (opzionale)
# =========================
app = FastAPI(title="Shopify FTP Image Sync") if FastAPI else None


class SyncResponse(BaseModel):
    scanned: int
    matched: int
    uploaded: int
    skipped: int
    errors: List[str]


# =========================
# Stato locale
# =========================
def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {"files": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log.warning("Impossibile leggere %s, riparto con stato vuoto", STATE_FILE)
        return {"files": {}}



def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# =========================
# Shopify auth
# =========================
_token_cache = {"value": None, "expires_at": 0.0}


def normalize_shop(shop: str) -> str:
    shop = shop.strip()
    if shop.startswith("https://"):
        shop = shop.replace("https://", "")
    if shop.endswith("/"):
        shop = shop[:-1]
    if not shop.endswith(".myshopify.com"):
        shop = f"{shop}.myshopify.com"
    return shop



def get_admin_access_token() -> str:
    now = time.time()
    if _token_cache["value"] and now < _token_cache["expires_at"] - 120:
        return _token_cache["value"]

    shop = normalize_shop(SHOPIFY_SHOP)
    if not shop or not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SECRET:
        raise RuntimeError("Credenziali Shopify mancanti")

    url = f"https://{shop}/admin/oauth/access_token"
    log.info("Richiedo access token Shopify per %s", shop)
    resp = requests.post(
        url,
        json={
            "client_id": SHOPIFY_CLIENT_ID,
            "client_secret": SHOPIFY_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data["access_token"]
    expires_in = data.get("expires_in", 86399)
    _token_cache["value"] = token
    _token_cache["expires_at"] = now + expires_in
    log.info("Access token ottenuto")
    return token



def shopify_graphql(query: str, variables: Optional[Dict] = None) -> Dict:
    shop = normalize_shop(SHOPIFY_SHOP)
    url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    token = get_admin_access_token()
    resp = requests.post(
        url,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": token,
        },
        json={"query": query, "variables": variables or {}},
        timeout=120,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload["data"]


# =========================
# FTP
# =========================
def ftp_connect() -> ftplib.FTP:
    if not FTP_HOST or not FTP_USER or not FTP_PASS:
        raise RuntimeError("Credenziali FTP mancanti")

    log.info("Connessione FTP a %s...", FTP_HOST)
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, 21, timeout=FTP_TIMEOUT)
    log.info("FTP connesso, login...")
    ftp.login(FTP_USER, FTP_PASS)
    ftp.set_pasv(FTP_PASSIVE)
    log.info("FTP login OK")
    return ftp



def ftp_walk_image_files() -> List[Tuple[str, str, int]]:
    """
    Ritorna lista di tuple: (directory, filename, size)
    Versione veloce: non usa ftp.size() per ogni file.
    Se TEST_MAX_FILES > 0, interrompe la scansione ai primi N file immagine validi.
    """
    log.info("Avvio scansione FTP...")
    results: List[Tuple[str, str, int]] = []
    ftp = ftp_connect()
    try:
        candidate_dirs = []
        if FTP_SPLIT_DIRS:
            for d in FTP_SPLIT_DIRS:
                candidate_dirs.append(f"{FTP_BASE_DIR.rstrip('/')}/{d}")
        else:
            candidate_dirs.append(FTP_BASE_DIR)

        log.info("Directory da scansionare: %s", candidate_dirs)
        if TEST_MAX_FILES > 0:
            log.info("TEST_MAX_FILES attivo: %s", TEST_MAX_FILES)

        for directory in candidate_dirs:
            log.info("Entro in %s", directory)
            try:
                ftp.cwd(directory)
                names = ftp.nlst()
                log.info("Trovati %s elementi in %s", len(names), directory)
            except Exception as e:
                log.exception("Errore su directory %s: %s", directory, e)
                continue

            added = 0
            for name in names:
                ext = os.path.splitext(name)[1].lower()
                if ext not in ALLOWED_EXTENSIONS:
                    continue

                # Non interrogo ftp.size(name): su molti server FTP rallenta drasticamente.
                results.append((directory, name, 0))
                added += 1

                if TEST_MAX_FILES > 0 and len(results) >= TEST_MAX_FILES:
                    log.info("Raggiunto TEST_MAX_FILES=%s", TEST_MAX_FILES)
                    log.info("File immagine validi in %s: %s", directory, added)
                    return results

            log.info("File immagine validi in %s: %s", directory, added)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass

    log.info("Scansione FTP completata. File immagine trovati: %s", len(results))
    return results

def ftp_read_file(directory: str, filename: str) -> bytes:
    log.info("Leggo file FTP %s/%s", directory, filename)
    ftp = ftp_connect()
    bio = BytesIO()
    try:
        ftp.cwd(directory)
        ftp.retrbinary(f"RETR {filename}", bio.write)
        data = bio.getvalue()
        log.info("File letto: %s (%s bytes)", filename, len(data))
        return data
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


# =========================
# Naming e fingerprint
# =========================
def parse_filename(filename: str) -> Optional[Dict]:
    base, ext = os.path.splitext(filename)
    ext = ext.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return None

    match = FILENAME_RE.match(base)
    if not match:
        return None

    sku = match.group("sku").strip()
    index_raw = match.group("index")
    index = 0 if index_raw is None else int(index_raw)

    return {
        "filename": filename,
        "sku": sku,
        "position": index,
        "extension": ext,
    }



def file_fingerprint(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# =========================
# Shopify queries/mutations
# =========================
def get_variant_by_sku(sku: str) -> Optional[Dict]:
    log.info("Cerco SKU su Shopify: %s", sku)
    query = """
    query GetVariantBySku($query: String!) {
      productVariants(first: 5, query: $query) {
        nodes {
          id
          sku
          title
          product {
            id
            title
            media(first: 100) {
              nodes {
                id
                alt
                mediaContentType
                ... on MediaImage {
                  image {
                    url
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    data = shopify_graphql(query, {"query": f"sku:{sku}"})
    nodes = data["productVariants"]["nodes"]
    for node in nodes:
        if (node.get("sku") or "").strip().lower() == sku.lower():
            log.info("SKU trovato: %s", sku)
            return node
    log.warning("SKU non trovato: %s", sku)
    return None



def staged_upload_create(filename: str, mime_type: str, file_size: int) -> Dict:
    log.info("Creo staged upload per %s", filename)
    mutation = """
    mutation StagedUploadsCreate($input: [StagedUploadInput!]!) {
      stagedUploadsCreate(input: $input) {
        stagedTargets {
          url
          resourceUrl
          parameters {
            name
            value
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    variables = {
        "input": [
            {
                "filename": filename,
                "mimeType": mime_type,
                "resource": "IMAGE",
                "httpMethod": "POST",
                "fileSize": str(file_size),
            }
        ]
    }
    data = shopify_graphql(mutation, variables)
    payload = data["stagedUploadsCreate"]
    if payload["userErrors"]:
        raise RuntimeError(payload["userErrors"])
    return payload["stagedTargets"][0]



def upload_to_staged_target(staged_target: Dict, content: bytes, filename: str, mime_type: str) -> str:
    log.info("Carico file su staged target: %s", filename)
    url = staged_target["url"]
    fields = {p["name"]: p["value"] for p in staged_target["parameters"]}
    files = {"file": (filename, content, mime_type)}
    resp = requests.post(url, data=fields, files=files, timeout=180)
    resp.raise_for_status()
    log.info("Upload completato: %s", filename)
    return staged_target["resourceUrl"]



def file_create_from_resource(resource_url: str, alt: str) -> str:
    log.info("Creo file Shopify da resource URL")
    mutation = """
    mutation FileCreate($files: [FileCreateInput!]!) {
      fileCreate(files: $files) {
        files {
          id
          fileStatus
          alt
          ... on MediaImage {
            image {
              url
            }
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    variables = {
        "files": [
            {
                "contentType": "IMAGE",
                "originalSource": resource_url,
                "alt": alt,
            }
        ]
    }
    data = shopify_graphql(mutation, variables)
    payload = data["fileCreate"]
    if payload["userErrors"]:
        raise RuntimeError(payload["userErrors"])
    file_id = payload["files"][0]["id"]
    log.info("File Shopify creato: %s", file_id)
    return file_id



def wait_file_ready(file_id: str, timeout_seconds: int = SHOPIFY_FILE_READY_TIMEOUT) -> None:
    log.info("Attendo READY per file %s", file_id)
    query = """
    query WaitFile($id: ID!) {
      node(id: $id) {
        ... on MediaImage {
          id
          fileStatus
          fileErrors {
            code
            details
            message
          }
        }
        ... on GenericFile {
          id
          fileStatus
          fileErrors {
            code
            details
            message
          }
        }
      }
    }
    """
    started = time.time()
    while time.time() - started < timeout_seconds:
        data = shopify_graphql(query, {"id": file_id})
        node = data.get("node")
        if not node:
            raise RuntimeError(f"File non trovato: {file_id}")
        status = node.get("fileStatus")
        log.info("file %s status=%s", file_id, status)
        if status == "READY":
            return
        if status == "FAILED":
            raise RuntimeError(f"File FAILED: {node.get('fileErrors')}")
        time.sleep(2)
    raise TimeoutError(f"Timeout attesa READY per {file_id}")



def attach_file_to_product(product_id: str, file_id: str, alt: str) -> None:
    log.info("Associo file %s al prodotto %s", file_id, product_id)
    mutation = """
    mutation AttachFileToProduct($input: ProductSetInput!) {
      productSet(input: $input) {
        product {
          id
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    variables = {
        "input": {
            "id": product_id,
            "files": [
                {
                    "originalSource": file_id,
                    "alt": alt,
                    "contentType": "IMAGE",
                }
            ],
        }
    }
    data = shopify_graphql(mutation, variables)
    errors = data["productSet"]["userErrors"]
    if errors:
        raise RuntimeError(errors)



def append_media_to_variant(product_id: str, variant_id: str, media_ids: List[str]) -> None:
    if not media_ids:
        return
    log.info("Associo %s media alla variante %s", len(media_ids), variant_id)
    mutation = """
    mutation AppendMediaToVariant($productId: ID!, $variantMedia: [ProductVariantAppendMediaInput!]!) {
      productVariantAppendMedia(productId: $productId, variantMedia: $variantMedia) {
        product {
          id
        }
        productVariants {
          id
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    variables = {
        "productId": product_id,
        "variantMedia": [
            {
                "variantId": variant_id,
                "mediaIds": media_ids,
            }
        ],
    }
    data = shopify_graphql(mutation, variables)
    errors = data["productVariantAppendMedia"]["userErrors"]
    if errors:
        raise RuntimeError(errors)



def reorder_product_media(product_id: str, ordered_media_ids: List[str]) -> None:
    if not ordered_media_ids:
        return
    log.info("Riordino media del prodotto %s", product_id)
    moves = [{"id": media_id, "newPosition": pos} for pos, media_id in enumerate(ordered_media_ids)]
    mutation = """
    mutation ReorderMedia($id: ID!, $moves: [MoveInput!]!) {
      productReorderMedia(id: $id, moves: $moves) {
        job {
          id
        }
        mediaUserErrors {
          field
          message
        }
      }
    }
    """
    data = shopify_graphql(mutation, {"id": product_id, "moves": moves})
    errors = data["productReorderMedia"]["mediaUserErrors"]
    if errors:
        raise RuntimeError(errors)


# =========================
# Core sync
# =========================
def sync_images() -> SyncResponse:
    log.info("Inizio sync_images()")
    state = load_state()
    scanned = matched = uploaded = skipped = 0
    errors: List[str] = []

    ftp_files = ftp_walk_image_files()
    scanned = len(ftp_files)
    log.info("File FTP letti: %s", scanned)

    grouped: Dict[str, List[Dict]] = {}
    for directory, filename, size in ftp_files:
        parsed = parse_filename(filename)
        if not parsed:
            skipped += 1
            continue
        item = {
            **parsed,
            "directory": directory,
            "size": size,
            "path_key": f"{directory}/{filename}",
        }
        grouped.setdefault(parsed["sku"], []).append(item)

    log.info("SKU raggruppati: %s", len(grouped))

    for sku, items in grouped.items():
        log.info("Elaboro SKU: %s | immagini: %s", sku, len(items))
        try:
            variant = get_variant_by_sku(sku)
            if not variant:
                errors.append(f"SKU non trovato su Shopify: {sku}")
                continue

            matched += 1
            product_id = variant["product"]["id"]
            variant_id = variant["id"]
            log.info("SKU %s trovato. Product=%s Variant=%s", sku, product_id, variant_id)

            items.sort(key=lambda x: (x["position"], x["filename"]))
            newly_added_media_ids: List[str] = []

            for item in items:
                path_key = item["path_key"]
                raw = ftp_read_file(item["directory"], item["filename"])
                fingerprint = file_fingerprint(raw)

                prev = state["files"].get(path_key)
                if prev and prev.get("fingerprint") == fingerprint:
                    log.info("File già sincronizzato, salto: %s", path_key)
                    skipped += 1
                    if prev.get("media_id"):
                        newly_added_media_ids.append(prev["media_id"])
                    continue

                mime_type = mimetypes.guess_type(item["filename"])[0] or "image/jpeg"
                alt = f"{sku} - {item['filename']}"

                staged = staged_upload_create(item["filename"], mime_type, len(raw))
                resource_url = upload_to_staged_target(staged, raw, item["filename"], mime_type)
                file_id = file_create_from_resource(resource_url, alt)
                wait_file_ready(file_id)
                attach_file_to_product(product_id, file_id, alt)
                newly_added_media_ids.append(file_id)
                uploaded += 1

                state["files"][path_key] = {
                    "sku": sku,
                    "fingerprint": fingerprint,
                    "media_id": file_id,
                    "uploaded_at": int(time.time()),
                }
                save_state(state)

            if newly_added_media_ids:
                append_media_to_variant(product_id, variant_id, newly_added_media_ids)
                try:
                    reorder_product_media(product_id, newly_added_media_ids)
                except Exception as e:
                    log.warning("Riordino media fallito per SKU %s: %s", sku, e)

        except Exception as exc:
            log.exception("Errore SKU %s: %s", sku, exc)
            errors.append(f"Errore SKU {sku}: {exc}")

    save_state(state)
    result = SyncResponse(
        scanned=scanned,
        matched=matched,
        uploaded=uploaded,
        skipped=skipped,
        errors=errors,
    )
    log.info("Sync completata: %s", result.model_dump())
    return result


# =========================
# FastAPI routes (solo se FastAPI disponibile)
# =========================
if app is not None:

    @app.get("/health")
    def health():
        return {"ok": True}


    @app.post("/sync", response_model=SyncResponse)
    def run_sync():
        required = {
            "SHOPIFY_SHOP": SHOPIFY_SHOP,
            "SHOPIFY_CLIENT_ID": SHOPIFY_CLIENT_ID,
            "SHOPIFY_CLIENT_SECRET": SHOPIFY_CLIENT_SECRET,
            "FTP_HOST": FTP_HOST,
            "FTP_USER": FTP_USER,
            "FTP_PASS": FTP_PASS,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise HTTPException(status_code=500, detail={"missing_env": missing})
        return sync_images()


# =========================
# CLI entrypoint per Render Cron Job
# =========================
def main() -> None:
    required = {
        "SHOPIFY_SHOP": SHOPIFY_SHOP,
        "SHOPIFY_CLIENT_ID": SHOPIFY_CLIENT_ID,
        "SHOPIFY_CLIENT_SECRET": SHOPIFY_CLIENT_SECRET,
        "FTP_HOST": FTP_HOST,
        "FTP_USER": FTP_USER,
        "FTP_PASS": FTP_PASS,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(f"Variabili ambiente mancanti: {missing}")

    result = sync_images()
    print(result.model_dump_json(indent=2), flush=True)


if __name__ == "__main__":
    main()
