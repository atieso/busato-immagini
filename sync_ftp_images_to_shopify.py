#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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
# Versione script (per verifica nei log)
# -----------------------
SCRIPT_VERSION = "2025-10-19-mlsd-v2"

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
FTP_BASE_DIR = (os.getenv("FTP_BASE_DIR", "/") or "/").rstrip("/")

FTP_USE_TLS = os.getenv("FTP_USE_TLS", "false").lower() == "true"

# Soluzione 1: spostare i file pubblicati
MOVE_AFTER_UPLOAD = os.getenv("MOVE_AFTER_UPLOAD", "false").lower() == "true"
FTP_PUBLISHED_DIR = os.getenv("FTP_PUBLISHED_DIR", "/Pubblicate").strip("/")
PUBLISHED_ROOT = (FTP_BASE_DIR + "/" + FTP_PUBLISHED_DIR).rstrip("/")

# Opzioni
ALSO_ATTACH_TO_VARIANT = os.getenv("ALSO_ATTACH_TO_VARIANT", "true").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# Estensioni consentite
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# Regex filename: SKU_index (index numerico)
FILENAME_RE = re.compile(r"^(?P<sku>.+?)_(?P<idx>\d+)$", re.IGNORECASE)


# -----------------------
# Util
# -----------------------
def to_numeric_id(gid: str) -> str:
    return gid.rsplit("/", 1)[-1]


def ftp_path_join(*parts: str) -> str:
    clean = []
    for p in parts:
        if p is None:
            continue
        p = str(p).strip().replace("\\", "/")
        if not p:
            continue
        clean.append(p.strip("/"))
    if not clean:
        return "/"
    return "/" + "/".join(clean)


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
        payload["image"]["variant_ids"] = [to_numeric_id(variant_id)]
    if DRY_RUN:
        LOG.info("[DRY RUN] Creerei image %s (position=%s, variant=%s)", alt, position, variant_id)
        return {"dry_run": True, "alt": alt, "position": position, "variant_id": variant_id}
    resp = requests.post(url, headers=headers, data=json.dumps(payload))
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Create image HTTP {resp.status_code}: {resp.text}")
    return resp.json()


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
    try:
        ftp.set_pasv(True)
    except Exception:
        pass
    return ftp


def ftp_reconnect() -> FTP:
    """Riconnette l'FTP usando le ENV correnti."""
    LOG.info("Riconnessione FTP...")
    return connect_ftp()


def ftp_ensure_dir(ftp: FTP, path: str):
    """
    Crea ricorsivamente una directory (se non esiste).
    """
    if not path or path == "/":
        return
    parts = [p for p in path.split("/") if p]
    cur = ""
    for p in parts:
        cur = ftp_path_join(cur, p)
        try:
            ftp.mkd(cur)
        except Exception:
            # esiste già
            pass


def ftp_move(ftp: FTP, src: str, dst: str):
    """
    Sposta (rename) un file, creando le cartelle di destinazione.
    """
    dst_dir = os.path.dirname(dst)
    ftp_ensure_dir(ftp, dst_dir)
    ftp.rename(src, dst)


def ftp_mlsd_safe(ftp: FTP, path: str):
    """
    Itera su MLSD restituendo (name, facts) senza fare cwd.
    Riprova una volta se il server chiude la connessione (EOFError).
    """
    # Prova API nativa mlsd (se il server la supporta)
    try:
        return list(ftp.mlsd(path, facts=["type", "size", "modify"]))
    except AttributeError:
        # ftplib ha mlsd; se siamo qui, il server può non supportare MLSD
        pass
    except EOFError:
        raise
    except error_perm:
        # Server che rifiuta MLSD: delega al fallback
        raise

    # Fallback: usare "MLSD path" via retrlines e parsare manualmente
    entries = []
    ftp.retrlines(f"MLSD {path}", entries.append)
    out = []
    for line in entries:
        # es.: "type=file;size=123;modify=20250101...; filename"
        if ";" in line:
            facts_part, name = line.rsplit(" ", 1)
            facts = {}
            for kv in facts_part.split(";"):
                kv = kv.strip()
                if not kv:
                    continue
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    facts[k] = v
            name = name.strip()
            out.append((name, facts))
        else:
            out.append((line.strip(), {}))
    return out


def walk_ftp_files(ftp: FTP, base_dir: str) -> List[str]:
    """
    Scansione robusta usando MLSD (senza cambiare directory).
    Esclude la cartella Pubblicate se MOVE_AFTER_UPLOAD=True.
    Gestione retry su EOFError con riconnessione trasparente.
    """
    base_dir = base_dir or "/"
    base_dir = base_dir.rstrip("/") or "/"

    files: List[str] = []
    exclude_root = PUBLISHED_ROOT if MOVE_AFTER_UPLOAD else None
    exclude_prefix = (exclude_root + "/") if exclude_root else None

    def is_excluded(path: str) -> bool:
        if not exclude_root:
            return False
        return path == exclude_root or path.startswith(exclude_prefix)

    stack = [base_dir]
    ops_since_noop = 0
    MAX_OPS_BEFORE_NOOP = 200  # keep-alive leggero

    while stack:
        current = stack.pop()
        if is_excluded(current):
            continue

        # keep-alive NOOP periodico per evitare timeout silenziosi
        try:
            ops_since_noop += 1
            if ops_since_noop >= MAX_OPS_BEFORE_NOOP:
                try:
                    ftp.voidcmd("NOOP")
                except Exception:
                    ftp = ftp_reconnect()
                ops_since_noop = 0
        except Exception:
            ftp = ftp_reconnect()
            ops_since_noop = 0

        # MLSD con retry
        entries = None
        for attempt in (1, 2):
            try:
                entries = ftp_mlsd_safe(ftp, current)
                break
            except EOFError:
                if attempt == 1:
                    LOG.warning("Connessione FTP chiusa durante MLSD su %s: riconnessione...", current)
                    ftp = ftp_reconnect()
                    continue
                else:
                    raise
            except error_perm:
                # Server non supporta MLSD: fallback dopo il loop
                entries = None
                break

        if entries is None:
            # Fallback: usa NLST per nomi, poi LIST per capire se dir/file (senza cwd)
            try:
                names = ftp.nlst(current)
            except EOFError:
                LOG.warning("Connessione FTP chiusa durante NLST su %s: riconnessione...", current)
                ftp = ftp_reconnect()
                names = ftp.nlst(current)

            # Normalizza a path completi
            norm_names = []
            for n in names:
                if not n:
                    continue
                if n.startswith(current.rstrip("/") + "/"):
                    norm_names.append(n)
                else:
                    norm_names.append(ftp_path_join(current, os.path.basename(n)))

            # Per ciascuno, prova LIST singola per capire se directory
            for full_path in norm_names:
                if is_excluded(full_path):
                    continue
                listing = []
                try:
                    ftp.retrlines(f"LIST {full_path}", listing.append)
                except EOFError:
                    LOG.warning("Connessione FTP chiusa durante LIST su %s: riconnessione...", full_path)
                    ftp = ftp_reconnect()
                    listing = []
                    ftp.retrlines(f"LIST {full_path}", listing.append)
                is_dir = False
                if listing:
                    first = listing[0].lower()
                    if first.startswith("d"):
                        is_dir = True
                if is_dir:
                    stack.append(full_path)
                else:
                    files.append(full_path)
            continue

        # Percorso MLSD con facts
        for name, facts in entries:
            if name in (".", ".."):
                continue
            full_path = ftp_path_join(current, name)
            if is_excluded(full_path):
                continue
            ftype = (facts.get("type") or "").lower()
            if ftype == "dir":
                stack.append(full_path)
            elif ftype == "file":
                files.append(full_path)
            else:
                # alcuni server usano valori non standard; trattiamo comunque come file
                files.append(full_path)

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
    base = os.path.basename(fname)
    name, ext = os.path.splitext(base)
    if ext.lower() not in IMAGE_EXTS:
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
        LOG.info("Scansione FTP da %s ...", FTP_BASE_DIR or "/")
        files = walk_ftp_files(ftp, FTP_BASE_DIR or "/")
        LOG.info("Trovati %d file totali su FTP (escludendo '%s')",
                 len(files), PUBLISHED_ROOT if MOVE_AFTER_UPLOAD else "N/A")
        groups = group_images_by_sku(files)
        LOG.info("Trovati %d SKU con immagini nel formato atteso", len(groups))
    finally:
        try:
            ftp.quit()
        except Exception:
            pass

    # 3) Upload ordinato + spostamento
    uploaded_count = 0
    skipped_unknown_sku = 0
    duplicated = 0
    errors = 0

    # ri-connetti per sicurezza
    ftp = connect_ftp()

    try:
        for sku, items in groups.items():
            if sku not in sku_map:
                skipped_unknown_sku += 1
                LOG.warning("SKU non presente in Shopify, salto: %s", sku)
                continue

            product_id, variant_id = sku_map[sku]
            try:
                existing_by_alt = list_existing_images(product_id)
            except Exception as e:
                existing_by_alt = {}
                LOG.error("Errore nel recupero immagini esistenti per %s: %s", sku, e)

            LOG.info("==> SKU %s | %d immagini da sincronizzare", sku, len(items))

            position_counter = 1
            for idx, path in items:
                alt = f"{sku}_{idx}"
                if alt in existing_by_alt:
                    duplicated += 1
                    LOG.info("  - già presente (alt=%s), salto", alt)
                    position_counter += 1
                    # Non spostiamo i duplicati per sicurezza
                    continue

                try:
                    content = ftp_read_file(ftp, path)
                    attachment_b64 = base64.b64encode(content).decode("ascii")
                    create_product_image(
                        product_id=product_id,
                        attachment_b64=attachment_b64,
                        alt=alt,
                        position=position_counter,
                        variant_id=variant_id if ALSO_ATTACH_TO_VARIANT else None
                    )
                    uploaded_count += 1
                    LOG.info("  + caricato %s (pos=%d)%s",
                             alt, position_counter, " [DRY RUN]" if DRY_RUN else "")
                    time.sleep(0.4)  # rate limit safety

                    # Move after successful upload (if enabled & not dry run)
                    if MOVE_AFTER_UPLOAD and not DRY_RUN:
                        # mantieni struttura relativa rispetto alla base
                        rel = path[len(FTP_BASE_DIR):] if path.startswith(FTP_BASE_DIR) else path
                        rel = rel.lstrip("/")
                        dst = ftp_path_join(FTP_BASE_DIR, FTP_PUBLISHED_DIR, rel)
                        try:
                            ftp_move(ftp, path, dst)
                            LOG.info("    ↪ spostato su %s", dst)
                        except Exception as me:
                            LOG.warning("    ⚠ impossibile spostare %s → %s: %s", path, dst, me)

                    position_counter += 1

                except Exception as e:
                    errors += 1
                    LOG.error("  ! errore su %s: %s", path, e)
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
    LOG.info("Script version: %s", SCRIPT_VERSION)
    LOG.info("Avvio sincronizzazione immagini FTP → Shopify (ordine _1, _2, ...)")
    LOG.info("DRY_RUN=%s | ALSO_ATTACH_TO_VARIANT=%s | MOVE_AFTER_UPLOAD=%s | PUBLISHED_DIR=%s",
             DRY_RUN, ALSO_ATTACH_TO_VARIANT, MOVE_AFTER_UPLOAD, PUBLISHED_ROOT)
    sync()
