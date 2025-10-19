import os
import io
import re
import time
import base64
import json
import logging
from ftplib import FTP, FTP_TLS, error_perm
from typing import Dict, List, Tuple, Optional
import requests

# -----------------------
# Config & logging
# -----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s | %(message)s"
)
LOG = logging.getLogger("ftp-shopify-sync")

SHOPIFY_STORE = os.getenv("SHOPIFY_STORE")  # es. city-tre-srl.myshopify.com
SHOPIFY_TOKEN = os.getenv("SHOPIFY_TOKEN")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-01")

FTP_HOST = os.getenv("FTP_HOST")
FTP_USER = os.getenv("FTP_USER")
FTP_PASSWORD = os.getenv("FTP_PASSWORD")
FTP_BASE_DIR = os.getenv("FTP_BASE_DIR", "/")
FTP_USE_TLS = os.getenv("FTP_USE_TLS", "false").lower() == "true"

# Opzioni
ALSO_ATTACH_TO_VARIANT = os.getenv("ALSO_ATTACH_TO_VARIANT", "true").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# Estensioni consentite
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# Regex filename: SKU_index (index numerico)
FILENAME_RE = re.compile(r"^(?P<sku>.+?)_(?P<idx>\d+)$", re.IGNORECASE)


# -----------------------
# Shopify helpers
# -----------------------
def shopify_graphql(query: str, variables: dict=None) -> dict:
    url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json",
    }
    resp = requests.post(url, headers=headers, json={"query": query, "variables": variables or {}})
    if resp.status_code != 200:
        raise RuntimeError(f"GraphQL HTTP {resp.status_code}: {resp.text}")
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data


def build_sku_index() -> Dict[str, Tuple[str, str]]:
    """
    Ritorna una mappa:
      sku -> (product_id, variant_id)
    Gli ID sono in formato GraphQL (gid://shopify/Product/..., gid://shopify/ProductVariant/...)
    """
    LOG.info("Costruisco indice SKU da Shopify (productVariants)...")
    query = """
    query($first:Int!, $after:String) {
      productVariants(first:$first, after:$after) {
        edges {
          cursor
          node {
            id
            sku
            product { id }
          }
        }
        pageInfo { hasNextPage }
      }
    }
    """
    sku_map: Dict[str, Tuple[str, str]] = {}
    after = None
    fetched = 0
    while True:
        data = shopify_graphql(query, {"first": 250, "after": after})
        edges = data["data"]["productVariants"]["edges"]
        for e in edges:
            node = e["node"]
            sku = (node["sku"] or "").strip()
            if not sku:
                continue
            product_id = node["product"]["id"]
            variant_id = node["id"]
            sku_map[sku] = (product_id, variant_id)
            fetched += 1
        if data["data"]["productVariants"]["pageInfo"]["hasNextPage"]:
            after = edges[-1]["cursor"]
            LOG.info("...caricati %d variants (continua)", fetched)
        else:
            break
    LOG.info("Indice SKU costruito: %d SKU mappati", len(sku_map))
    return sku_map


def list_existing_images(product_id: str) -> Dict[str, dict]:
    """
    Restituisce dict alt_text -> image_dict per un prodotto,
    così evitiamo duplicati basati su alt (es. SKU_1, SKU_2, ...).
    """
    # GraphQL per immagini prodotto
    query = """
    query($id: ID!) {
      product(id: $id) {
        images(first: 250) {
          edges {
            node {
              id
              altText
              position
              src
              width
              height
            }
          }
        }
      }
    }
    """
    data = shopify_graphql(query, {"id": product_id})
    images = {}
    edges = data["data"]["product"]["images"]["edges"]
    for e in edges:
        node = e["node"]
        alt = (node.get("altText") or "").strip()
        if alt:
            images[alt] = node
    return images


def create_product_image(product_id: str, attachment_b64: str, alt: str, position: Optional[int]=None, variant_id: Optional[str]=None) -> dict:
    """
    Crea un'immagine su un prodotto via REST, con 'attachment' base64.
    Opzionalmente setta alt, position, e associa variant_id.
    """
    url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/products/{to_numeric_id(product_id)}/images.json"
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {
        "image": {
            "attachment": attachment_b64,
            "alt": alt,
        }
    }
    if position is not None:
        payload["image"]["position"] = position
    if ALSO_ATTACH_TO_VARIANT and variant_id:
        # per associare l'immagine alla variant corretta
        payload["image"]["variant_ids"] = [to_numeric_id(variant_id)]
    if DRY_RUN:
        LOG.info("[DRY RUN] Creerei image %s (position=%s, variant=%s)", alt, position, variant_id)
        return {"dry_run": True, "alt": alt, "position": position, "variant_id": variant_id}
    resp = requests.post(url, headers=headers, data=json.dumps(payload))
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Create image HTTP {resp.status_code}: {resp.text}")
    return resp.json()


def to_numeric_id(gid: str) -> str:
    """
    Converte un gid://shopify/Resource/1234567890 in "1234567890"
    """
    return gid.rsplit("/", 1)[-1]


# -----------------------
# FTP helpers
# -----------------------
def connect_ftp() -> FTP:
    if FTP_USE_TLS:
        ftp = FTP_TLS(FTP_HOST)
        ftp.login(FTP_USER, FTP_PASSWORD)
        ftp.prot_p()
    else:
        ftp = FTP(FTP_HOST)
        ftp.login(FTP_USER, FTP_PASSWORD)
    return ftp


def walk_ftp_files(ftp: FTP, base_dir: str) -> List[str]:
    """
    Ritorna la lista dei path dei file (relativi) sotto base_dir.
    """
    files = []

    def _walk(cwd: str):
        try:
            entries = []
            ftp.retrlines(f"MLSD {cwd}", entries.append)
            # MLSD fornisce tipo; se non supportato, fallback LIST
            for line in entries:
                # Esempio MLSD: "type=file;size=123;modify=20250101...; unique=...; filename"
                parts, name = line.split(";", 1)[-1].strip().split(None, 1) if ";" in line else ("", line.strip())
                # Purtroppo parsing MLSD varia; usiamo un fallback semplice:
                name = name.strip()
                if name in (".", ".."):
                    continue
                full_path = f"{cwd.rstrip('/')}/{name}"
                # Determinare se file o dir: tentiamo CWD
                try:
                    cwd_before = ftp.pwd()
                    ftp.cwd(full_path)
                    ftp.cwd(cwd_before)
                    # è directory
                    _walk(full_path)
                except error_perm:
                    # è file
                    files.append(full_path)
        except error_perm:
            # MLSD non supportato, usiamo LIST
            listing = []
            ftp.retrlines(f"LIST {cwd}", listing.append)
            for line in listing:
                # formato: drwxr-xr-x 1 user group size date name
                parts = line.split()
                if len(parts) < 9:
                    continue
                name = " ".join(parts[8:])
                if name in (".", ".."):
                    continue
                full_path = f"{cwd.rstrip('/')}/{name}"
                if line.lower().startswith("d"):
                    _walk(full_path)
                else:
                    files.append(full_path)

    _walk(base_dir)
    return files


def ftp_read_file(ftp: FTP, path: str) -> bytes:
    bio = io.BytesIO()
    ftp.retrbinary(f"RETR {path}", bio.write)
    return bio.getvalue()


# -----------------------
# Core
# -----------------------
def parse_filename(fname: str) -> Optional[Tuple[str, int]]:
    """
    Ritorna (sku, idx) se il filename (senza estensione) è nel formato SKU_idx
    """
    name, ext = os.path.splitext(os.path.basename(fname))
    ext = ext.lower()
    if ext not in IMAGE_EXTS:
        return None
    m = FILENAME_RE.match(name)
    if not m:
        return None
    sku = m.group("sku").strip()
    idx = int(m.group("idx"))
    return sku, idx


def group_images_by_sku(file_paths: List[str]) -> Dict[str, List[Tuple[int, str]]]:
    groups: Dict[str, List[Tuple[int, str]]] = {}
    for path in file_paths:
        parsed = parse_filename(path)
        if not parsed:
            continue
        sku, idx = parsed
        groups.setdefault(sku, []).append((idx, path))
    # ordina ciascun gruppo per idx
    for sku in groups:
        groups[sku].sort(key=lambda x: x[0])
    return groups


def sync():
    # 1) Indice SKU
    if not (SHOPIFY_STORE and SHOPIFY_TOKEN):
        raise SystemExit("Config mancante: SHOPIFY_STORE / SHOPIFY_TOKEN")
    sku_map = build_sku_index()

    # 2) Connessione FTP e discovery
    if not (FTP_HOST and FTP_USER and FTP_PASSWORD):
        raise SystemExit("Config mancante: FTP_HOST / FTP_USER / FTP_PASSWORD")
    ftp = connect_ftp()
    try:
        LOG.info("Scansione FTP da %s ...", FTP_BASE_DIR)
        files = walk_ftp_files(ftp, FTP_BASE_DIR)
        LOG.info("Trovati %d file totali su FTP", len(files))
        groups = group_images_by_sku(files)
        LOG.info("Trovati %d SKU con immagini nel formato atteso", len(groups))
    finally:
        try:
            ftp.quit()
        except Exception:
            pass

    # 3) Per ogni SKU presente sia su FTP che su Shopify, carica in ordine
    uploaded_count = 0
    skipped_unknown_sku = 0
    duplicated = 0
    errors = 0

    # Ri-connetti FTP per download (alcuni server chiudono dopo LIST lunghe)
    ftp = connect_ftp()

    try:
        for sku, items in groups.items():
            if sku not in sku_map:
                skipped_unknown_sku += 1
                LOG.warning("SKU non presente in Shopify, salto: %s", sku)
                continue

            product_id, variant_id = sku_map[sku]
            existing_by_alt = {}
            try:
                existing_by_alt = list_existing_images(product_id)
            except Exception as e:
                LOG.error("Errore nel recupero immagini esistenti per %s: %s", sku, e)

            LOG.info("==> SKU %s | %d immagini da sincronizzare", sku, len(items))

            position_counter = 1
            for idx, path in items:
                alt = f"{sku}_{idx}"
                if alt in existing_by_alt:
                    duplicated += 1
                    LOG.info("  - già presente (alt=%s), salto", alt)
                    position_counter += 1
                    continue

                try:
                    content = ftp_read_file(ftp, path)
                    attachment_b64 = base64.b64encode(content).decode("ascii")
                    resp = create_product_image(
                        product_id=product_id,
                        attachment_b64=attachment_b64,
                        alt=alt,
                        position=position_counter,
                        variant_id=variant_id if ALSO_ATTACH_TO_VARIANT else None
                    )
                    uploaded_count += 1
                    LOG.info("  + caricato %s (pos=%d)%s",
                             alt,
                             position_counter,
                             " [DRY RUN]" if DRY_RUN else "")
                    # rispetto limiti API
                    time.sleep(0.4)  # ~2.5 rps
                    position_counter += 1
                except Exception as e:
                    errors += 1
                    LOG.error("  ! errore su %s: %s", path, e)
                    # backoff leggero
                    time.sleep(1.0)

    finally:
        try:
            ftp.quit()
        except Exception:
            pass

    LOG.info("----- RIEPILOGO -----")
    LOG.info("Caricate: %d", uploaded_count)
    LOG.info("Duplicati (skip): %d", duplicated)
    LOG.info("SKU non trovati: %d", skipped_unknown_sku)
    LOG.info("Errori: %d", errors)


if __name__ == "__main__":
    LOG.info("Avvio sincronizzazione immagini FTP → Shopify (ordine _1, _2, ...)")
    LOG.info("DRY_RUN=%s | ALSO_ATTACH_TO_VARIANT=%s", DRY_RUN, ALSO_ATTACH_TO_VARIANT)
    sync()
