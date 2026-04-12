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

FTP_AFTER_SYNC_ACTION = os.getenv("FTP_AFTER_SYNC_ACTION", "move").strip().lower()
FTP_PUBLISHED_DIR = os.getenv("FTP_PUBLISHED_DIR", "").strip()
FTP_RENAMED_PREFIX = os.getenv("FTP_RENAMED_PREFIX", "_SYNCED_").strip() or "_SYNCED_"
FTP_RENAMED_SUFFIX = os.getenv("FTP_RENAMED_SUFFIX", "").strip()

media_hash_cache: Dict[str, str] = {}
product_media_cache: Dict[str, List[Dict]] = {}


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


def ftp_path_join(*parts: str) -> str:
    cleaned = [p.strip("/") for p in parts if p not in (None, "")]
    if not cleaned:
        return "/"
    return "/" + "/".join(cleaned)


def ftp_ensure_dir(ftp: ftplib.FTP, path: str) -> None:
    path = path.strip()
    if not path or path == "/":
        return

    current = ""
    for part in [p for p in path.strip("/").split("/") if p]:
        current = f"{current}/{part}" if current else f"/{part}"
        try:
            ftp.cwd(current)
        except Exception:
            try:
                ftp.mkd(current)
            except Exception:
                # Se la directory esiste già o il server risponde in modo non standard, riprova a entrarci
                pass
            ftp.cwd(current)


def ftp_archive_file(directory: str, filename: str) -> Dict[str, str]:
    """
    Dopo una sync riuscita:
    - move  -> sposta il file in FTP_PUBLISHED_DIR[/subdir]
    - rename -> rinomina il file nella stessa directory
    - none -> non fa nulla
    """
    action = FTP_AFTER_SYNC_ACTION
    if action in ("", "none", "off", "false", "0"):
        return {"action": "none", "path": f"{directory}/{filename}"}

    ftp = ftp_connect()
    try:
        ftp.cwd(directory)

        if action == "move":
            if not FTP_PUBLISHED_DIR:
                raise RuntimeError("FTP_PUBLISHED_DIR mancante per FTP_AFTER_SYNC_ACTION=move")

            subdir = directory.rstrip("/").split("/")[-1]
            target_dir = ftp_path_join(FTP_PUBLISHED_DIR, subdir) if subdir.isdigit() else FTP_PUBLISHED_DIR
            ftp_ensure_dir(ftp, target_dir)

            target_name = filename
            target_path = ftp_path_join(target_dir, target_name)

            if _ftp_file_exists(ftp, target_path):
                stamp = time.strftime("%Y%m%d-%H%M%S")
                base, ext = os.path.splitext(filename)
                target_name = f"{base}__{stamp}{ext}"
                target_path = ftp_path_join(target_dir, target_name)

            source_path = ftp_path_join(directory, filename)
            ftp.rename(source_path, target_path)
            log.info("File FTP spostato: %s -> %s", source_path, target_path)
            return {"action": "move", "path": target_path}

        if action == "rename":
            stamp = time.strftime("%Y%m%d-%H%M%S")
            base, ext = os.path.splitext(filename)
            new_name = f"{FTP_RENAMED_PREFIX}{base}{FTP_RENAMED_SUFFIX}"
            if not FTP_RENAMED_SUFFIX:
                new_name = f"{new_name}__{stamp}"
            new_name = f"{new_name}{ext}"

            source_path = ftp_path_join(directory, filename)
            target_path = ftp_path_join(directory, new_name)
            ftp.rename(source_path, target_path)
            log.info("File FTP rinominato: %s -> %s", source_path, target_path)
            return {"action": "rename", "path": target_path}

        raise RuntimeError(f"FTP_AFTER_SYNC_ACTION non supportata: {action}")

    finally:
        try:
            ftp.quit()
        except Exception:
            pass


def _ftp_file_exists(ftp: ftplib.FTP, absolute_path: str) -> bool:
    parent = "/" + "/".join([p for p in absolute_path.strip("/").split("/")[:-1]])
    name = absolute_path.strip("/").split("/")[-1]
    original_pwd = ftp.pwd()
    try:
        ftp.cwd(parent or "/")
        return name in ftp.nlst()
    except Exception:
        return False
    finally:
        try:
            ftp.cwd(original_pwd)
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


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download_bytes(url: str) -> bytes:
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    return resp.content


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



def get_product_media_images(product_id: str) -> List[Dict]:
    """
    Restituisce tutte le immagini media del prodotto.
    """
    if product_id in product_media_cache:
        return product_media_cache[product_id]

    query = """
    query GetProductMediaImages($id: ID!) {
      product(id: $id) {
        id
        media(first: 250) {
          nodes {
            id
            alt
            mediaContentType
            ... on MediaImage {
              image {
                url
              }
              originalSource {
                url
                fileSize
              }
            }
          }
        }
      }
    }
    """
    data = shopify_graphql(query, {"id": product_id})
    nodes = data["product"]["media"]["nodes"]
    images = [node for node in nodes if node.get("mediaContentType") == "IMAGE"]
    log.info("Media immagine attuali sul prodotto %s: %s", product_id, len(images))
    product_media_cache[product_id] = images
    return images


def get_media_hash(media_node: Dict) -> Optional[str]:
    media_id = media_node["id"]
    if media_id in media_hash_cache:
        return media_hash_cache[media_id]

    original_url = ((media_node.get("originalSource") or {}).get("url"))
    fallback_url = ((media_node.get("image") or {}).get("url"))
    source_url = original_url or fallback_url
    if not source_url:
        return None

    try:
        data = download_bytes(source_url)
        h = sha256_bytes(data)
        media_hash_cache[media_id] = h
        return h
    except Exception as exc:
        log.warning("Impossibile calcolare hash media %s: %s", media_id, exc)
        return None


def invalidate_product_media_cache(product_id: str, media_ids: Optional[List[str]] = None) -> None:
    product_media_cache.pop(product_id, None)
    if media_ids:
        for media_id in media_ids:
            media_hash_cache.pop(media_id, None)


def find_existing_media_and_duplicates(product_id: str, ftp_bytes: bytes, expected_alt: str) -> Tuple[Optional[str], List[str]]:
    """
    Cerca nel prodotto un'immagine già presente.
    Priorità:
    1) stesso ALT (più affidabile per immagini caricate da questa app)
    2) stesso hash bytes come fallback
    """
    images = get_product_media_images(product_id)

    alt_matches = [
        m for m in images
        if (m.get("alt") or "").strip() == expected_alt.strip()
    ]
    if alt_matches:
        keep_node = alt_matches[0]
        duplicate_ids = [m["id"] for m in alt_matches[1:]]
        log.info(
            "Match per ALT trovato per %s: keep=%s duplicati=%s",
            expected_alt,
            keep_node["id"],
            duplicate_ids,
        )
        return keep_node["id"], duplicate_ids

    target_hash = sha256_bytes(ftp_bytes)
    hash_matches: List[Dict] = []
    for media_node in images:
        media_hash = get_media_hash(media_node)
        if media_hash and media_hash == target_hash:
            hash_matches.append(media_node)

    if not hash_matches:
        log.info("Nessun match esistente trovato per %s", expected_alt)
        return None, []

    keep_node = hash_matches[0]
    duplicate_ids = [m["id"] for m in hash_matches[1:] if m["id"] != keep_node["id"]]
    log.info(
        "Match per HASH trovato per %s: keep=%s duplicati=%s",
        expected_alt,
        keep_node["id"],
        duplicate_ids,
    )
    return keep_node["id"], duplicate_ids


def delete_duplicate_product_media(product_id: str, media_ids: List[str]) -> None:
    if not media_ids:
        return

    unique_ids = list(dict.fromkeys(media_ids))
    log.info("Elimino media duplicati dal prodotto %s: %s", product_id, unique_ids)

    mutation = """
    mutation DeleteDuplicateMedia($productId: ID!, $mediaIds: [ID!]!) {
      productDeleteMedia(productId: $productId, mediaIds: $mediaIds) {
        deletedMediaIds
        mediaUserErrors {
          field
          message
        }
      }
    }
    """
    data = shopify_graphql(mutation, {"productId": product_id, "mediaIds": unique_ids})
    payload = data["productDeleteMedia"]
    errors = payload["mediaUserErrors"]
    if errors:
        raise RuntimeError(errors)

    log.info("Media eliminati davvero dal prodotto: %s", payload.get("deletedMediaIds", []))
    invalidate_product_media_cache(product_id, unique_ids)


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



def get_file_cdn_url(file_id: str) -> str:
    log.info("Recupero CDN URL per file %s", file_id)
    query = """
    query GetFileCdnUrl($id: ID!) {
      node(id: $id) {
        ... on MediaImage {
          id
          fileStatus
          image {
            url
          }
          preview {
            image {
              url
            }
          }
        }
        ... on GenericFile {
          id
          fileStatus
          preview {
            image {
              url
            }
          }
        }
      }
    }
    """
    data = shopify_graphql(query, {"id": file_id})
    node = data.get("node")
    if not node:
        raise RuntimeError(f"File non trovato: {file_id}")

    cdn_url = (
        ((node.get("image") or {}).get("url"))
        or (((node.get("preview") or {}).get("image") or {}).get("url"))
    )

    if not cdn_url:
        raise RuntimeError(f"CDN URL non trovata per file {file_id}")

    log.info("CDN URL recuperata per %s", file_id)
    return cdn_url



def attach_media_to_product(product_id: str, source_url: str, alt: str) -> str:
    log.info("Associo media al prodotto %s tramite URL CDN", product_id)
    mutation = """
    mutation UpdateProductWithMedia($product: ProductUpdateInput!, $media: [CreateMediaInput!]) {
      productUpdate(product: $product, media: $media) {
        product {
          id
          media(first: 20) {
            nodes {
              id
              alt
              mediaContentType
              ... on MediaImage {
                image {
                  url
                }
                preview {
                  status
                }
              }
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
        "product": {"id": product_id},
        "media": [
            {
                "originalSource": source_url,
                "alt": alt,
                "mediaContentType": "IMAGE",
            }
        ],
    }
    data = shopify_graphql(mutation, variables)
    payload = data["productUpdate"]
    if payload["userErrors"]:
        raise RuntimeError(payload["userErrors"])

    nodes = payload["product"]["media"]["nodes"]
    for node in reversed(nodes):
        if node.get("alt") == alt:
            media_id = node["id"]
            log.info("Media associato al prodotto: %s", media_id)
            return media_id
        image_url = ((node.get("image") or {}).get("url"))
        if image_url == source_url:
            media_id = node["id"]
            log.info("Media associato al prodotto: %s", media_id)
            return media_id

    raise RuntimeError("Media associato al prodotto ma ID non trovato nel payload")



def attach_media_to_variant(product_id: str, variant_id: str, media_id: str) -> None:
    log.info("Associo media %s alla variante %s", media_id, variant_id)
    mutation = """
    mutation UpdateVariantMedia($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
      productVariantsBulkUpdate(productId: $productId, variants: $variants) {
        product {
          id
        }
        productVariants {
          id
          media(first: 10) {
            nodes {
              id
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
        "productId": product_id,
        "variants": [
            {
                "id": variant_id,
                "mediaId": media_id,
            }
        ],
    }
    data = shopify_graphql(mutation, variables)
    errors = data["productVariantsBulkUpdate"]["userErrors"]
    if errors:
        raise RuntimeError(errors)



def reorder_product_media(product_id: str, ordered_media_ids: List[str]) -> None:
    if not ordered_media_ids:
        return
    log.info("Riordino media del prodotto %s", product_id)
    moves = [{"id": media_id, "newPosition": str(pos)} for pos, media_id in enumerate(ordered_media_ids)]
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
                mime_type = mimetypes.guess_type(item["filename"])[0] or "image/jpeg"
                alt = f"{sku} - {item['filename']}"

                existing_media_id, duplicate_media_ids = find_existing_media_and_duplicates(product_id, raw, alt)
                if existing_media_id:
                    log.info("Immagine già presente sul prodotto. Riuso media %s", existing_media_id)
                    newly_added_media_ids.append(existing_media_id)
                    attach_media_to_variant(product_id, variant_id, existing_media_id)

                    if duplicate_media_ids:
                        log.info("Trovati duplicati da eliminare: %s", duplicate_media_ids)
                        delete_duplicate_product_media(product_id, duplicate_media_ids)

                    archive_info = ftp_archive_file(item["directory"], item["filename"])

                    state["files"][path_key] = {
                        "sku": sku,
                        "fingerprint": fingerprint,
                        "file_id": None,
                        "media_id": existing_media_id,
                        "uploaded_at": int(time.time()),
                        "ftp_after_sync_action": archive_info["action"],
                        "ftp_after_sync_path": archive_info["path"],
                    }
                    save_state(state)
                    skipped += 1
                    continue

                prev = state["files"].get(path_key)
                if prev and prev.get("fingerprint") == fingerprint:
                    log.info("File già sincronizzato in stato locale ma non trovato sul prodotto, ricarico: %s", path_key)

                staged = staged_upload_create(item["filename"], mime_type, len(raw))
                resource_url = upload_to_staged_target(staged, raw, item["filename"], mime_type)
                file_id = file_create_from_resource(resource_url, alt)
                wait_file_ready(file_id)

                cdn_url = get_file_cdn_url(file_id)
                log.info("CDN URL file READY: %s", cdn_url)

                product_media_id = attach_media_to_product(product_id, cdn_url, alt)
                invalidate_product_media_cache(product_id)

                # Pulizia duplicati eventuali dopo il nuovo upload
                keep_media_id, duplicate_media_ids = find_existing_media_and_duplicates(product_id, raw, alt)
                final_media_id = keep_media_id or product_media_id
                if duplicate_media_ids:
                    duplicate_media_ids = [m for m in duplicate_media_ids if m != final_media_id]
                    if duplicate_media_ids:
                        delete_duplicate_product_media(product_id, duplicate_media_ids)

                newly_added_media_ids.append(final_media_id)
                archive_info = ftp_archive_file(item["directory"], item["filename"])
                uploaded += 1

                state["files"][path_key] = {
                    "sku": sku,
                    "fingerprint": fingerprint,
                    "file_id": file_id,
                    "media_id": final_media_id,
                    "uploaded_at": int(time.time()),
                    "ftp_after_sync_action": archive_info["action"],
                    "ftp_after_sync_path": archive_info["path"],
                }
                save_state(state)

            if newly_added_media_ids:
                ordered_unique_media_ids = list(dict.fromkeys(newly_added_media_ids))
                for media_id in ordered_unique_media_ids:
                    attach_media_to_variant(product_id, variant_id, media_id)
                if len(ordered_unique_media_ids) > 1:
                    try:
                        reorder_product_media(product_id, ordered_unique_media_ids)
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
