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
# Versione script
# -----------------------
SCRIPT_VERSION = "2025-10-19-mlsd-v6-split-dirs"

# -----------------------
# Config & logging
# -----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s | %(message)s"
)
LOG = logging.getLogger("ftp-shopify-sync")

# Shopify
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE")
SHOPIFY_TOKEN = os.getenv("SHOPIFY_TOKEN")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-01")

# FTP
FTP_HOST = os.getenv("FTP_HOST")
FTP_USER = os.getenv("FTP_USER")
FTP_PASSWORD = os.getenv("FTP_PASSWORD")
FTP_BASE_DIR = (os.getenv("FTP_BASE_DIR", "/") or "/").rstrip("/")
FTP_USE_TLS = os.getenv("FTP_USE_TLS", "false").lower() == "true"

# suddivisione in sottocartelle, es: "0,1,2,3,4,5,6,7,8,9"
FTP_SPLIT_DIRS = os.getenv("FTP_SPLIT_DIRS", "").strip()

# Spostamento post-upload
MOVE_AFTER_UPLOAD = os.getenv("MOVE_AFTER_UPLOAD", "false").lower() == "true"
FTP_PUBLISHED_DIR = os.getenv("FTP_PUBLISHED_DIR", "/Pubblicate").strip("/")
PUBLISHED_ROOT = (FTP_BASE_DIR + "/" + FTP_PUBLISHED_DIR).rstrip("/")

# altro
ALSO_ATTACH_TO_VARIANT = os.getenv("ALSO_ATTACH_TO_VARIANT", "true").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# Estensioni consentite
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# Filename: "SKU" oppure "SKU_2"
FILENAME_RE = re.compile(r"^(?P<sku>.+?)(?:_(?P<idx>\d+))?$", re.IGNORECASE)


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
    Restituisce dict alt_text -> image_dict per evitare ricariche.
    """
    query = """
    query($id: ID!) {
      product(id: $id) {
        images(first: 250) {
          edges {
            node {
              id
              altText
              url
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


def create_product_image(product_id: str, attachment_b64: str, alt: str,
                         position: Optional[int] = None,
                         variant_id: Optional[str] = None) -> dict:
    url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/products/{to_numeric_id(product_id)}/images.json"
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {"image": {"attachment": attachment_b64, "alt": alt}}
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
    data = resp.json()
    try:
        img = data.get("image") or {}
        LOG.info("    ✓ Shopify created image id=%s src=%s alt=%s",
                 img.get("id"), img.get("src"), alt)
    except Exception:
        pass
    return data


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
    LOG.info("Riconnessione FTP...")
    return connect_ftp()


def ftp_ensure_dir(ftp: FTP, path: str):
    if not path or path == "/":
        return
    parts = [p for p in path.split("/") if p]
    cur = ""
    for p in parts:
        cur = ftp_path_join(cur, p)
        try:
            ftp.mkd(cur)
        except Exception:
            pass


def ftp_move(ftp: FTP, src: str, dst: str):
    dst_dir = os.path.dirname(dst)
    ftp_ensure_dir(ftp, dst_dir)
    try:
        ftp.rename(src, dst)
        return
    except Exception as e1:
        LOG.warning("    rename assoluto fallito (%s). Provo fallback relativo.", e1)

    cwd0 = ftp.pwd()
    try:
        base = FTP_BASE_DIR or "/"
        ftp.cwd(base)

        rel_src = src[len(base):].lstrip("/") if src.startswith(base) else src.lstrip("/")
        rel_dst = dst[len(base):].lstrip("/") if dst.startswith(base) else dst.lstrip("/")

        rel_dst_dir = os.path.dirname(rel_dst)
        ftp_ensure_dir(ftp, rel_dst_dir)
        ftp.rename(rel_src, rel_dst)
    finally:
        try:
            ftp.cwd(cwd0)
        except Exception:
            pass


def ftp_mlsd_safe(ftp: FTP, path: str):
    try:
        return list(ftp.mlsd(path, facts=["type", "size", "modify"]))
    except AttributeError:
        pass
    except EOFError:
        raise
    except error_perm:
        raise

    entries = []
    ftp.retrlines(f"MLSD {path}", entries.append)
    out = []
    for line in entries:
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
    MAX_OPS_BEFORE_NOOP = 200

    while stack:
        current = stack.pop()
        if is_excluded(current):
            continue

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
                entries = None
                break

        if entries is None:
            try:
                names = ftp.nlst(current)
            except EOFError:
                LOG.warning("Connessione FTP chiusa durante NLST su %s: riconnessione...", current)
                ftp = ftp_reconnect()
                names = ftp.nlst(current)

            norm_names = []
            for n in names:
                if not n:
                    continue
                if n.startswith(current.rstrip("/") + "/"):
                    norm_names.append(n)
                else:
                    norm_names.append(ftp_path_join(current, os.path.basename(n)))

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
                if listing and listing[0].lower().startswith("d"):
                    is_dir = True
                if is_dir:
                    stack.append(full_path)
                else:
                    files.append(full_path)
            continue

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
                files.append(full_path)

    return files


def ftp_read_file(ftp: FTP, path: str) -> bytes:
    """
    Scarica un file via FTP con fino a 3 tentativi automatici
    in caso di disconnessione (Broken pipe, EOF, ecc.)
    """
    attempts = 0
    while attempts < 3:
        bio = io.BytesIO()
        try:
            ftp.retrbinary(f"RETR {path}", bio.write)
            return bio.getvalue()
        except (EOFError, OSError, ConnectionResetError, BrokenPipeError) as e:
            attempts += 1
            LOG.warning("Connessione interrotta durante RETR %s (tentativo %d/3): %s",
                        path, attempts, e)
            try:
                ftp = ftp_reconnect()
            except Exception as re:
                LOG.warning("Errore nella riconnessione FTP: %s", re)
            time.sleep(2)

    # dopo 3 tentativi falliti, lo segnaliamo come errore "definitivo"
    raise RuntimeError(f"Impossibile leggere {path} dopo 3 tentativi (Broken pipe)")



# -----------------------
# Analisi
# -----------------------
def parse_filename_strict(path: str):
    base = os.path.basename(path)
    name, ext = os.path.splitext(base)
    ext = ext.lower()
    if ext not in IMAGE_EXTS:
        return {"ok": False, "reason": "bad_ext"}

    m = FILENAME_RE.match(name)
    if not m:
        return {"ok": False, "reason": "bad_pattern"}

    sku = (m.group("sku") or "").strip()
    if not sku:
        return {"ok": False, "reason": "bad_pattern"}

    idx_str = m.group("idx")
    if idx_str is None:
        idx = 1
    else:
        try:
            idx = int(idx_str)
        except Exception:
            return {"ok": False, "reason": "bad_pattern"}
        if idx < 1:
            return {"ok": False, "reason": "bad_pattern"}

    return {"ok": True, "sku": sku, "idx": idx}


def analyze_files(files: List[str], sku_map: Dict[str, Tuple[str, str]], max_examples: int = 10):
    stats = {
        "total_files": len(files),
        "bad_ext": 0,
        "bad_pattern": 0,
        "ok_parse": 0,
        "ok_parse_unique_skus": set(),
        "ok_parse_sku_not_in_shopify": 0,
        "ok_parse_sku_in_shopify": 0,
        "examples_bad_ext": [],
        "examples_bad_pattern": [],
        "examples_sku_not_in_shopify": [],
    }

    for path in files:
        p = parse_filename_strict(path)
        if not p["ok"]:
            if p["reason"] == "bad_ext":
                stats["bad_ext"] += 1
                if len(stats["examples_bad_ext"]) < max_examples:
                    stats["examples_bad_ext"].append(os.path.basename(path))
            else:
                stats["bad_pattern"] += 1
                if len(stats["examples_bad_pattern"]) < max_examples:
                    stats["examples_bad_pattern"].append(os.path.basename(path))
            continue

        stats["ok_parse"] += 1
        stats["ok_parse_unique_skus"].add(p["sku"])

        if p["sku"] in sku_map:
            stats["ok_parse_sku_in_shopify"] += 1
        else:
            stats["ok_parse_sku_not_in_shopify"] += 1
            if len(stats["examples_sku_not_in_shopify"]) < max_examples:
                stats["examples_sku_not_in_shopify"].append(p["sku"])

    LOG.info("ANALISI FTP — Totale file: %d", stats["total_files"])
    LOG.info("  • File con estensione NON valida: %d", stats["bad_ext"])
    if stats["examples_bad_ext"]:
        LOG.info("    esempi bad_ext: %s", stats["examples_bad_ext"])
    LOG.info("  • File con NOME non conforme (pattern SKU o SKU_#): %d", stats["bad_pattern"])
    if stats["examples_bad_pattern"]:
        LOG.info("    esempi bad_pattern: %s", stats["examples_bad_pattern"])
    LOG.info("  • File validi (pattern ok): %d", stats["ok_parse"])
    LOG.info("    - SKUs distinti trovati nei file validi: %d", len(stats["ok_parse_unique_skus"]))
    LOG.info("    - di cui con SKU NON presenti su Shopify: %d", stats["ok_parse_sku_not_in_shopify"])
    if stats["examples_sku_not_in_shopify"]:
        LOG.info("      esempi SKU non trovati: %s", stats["examples_sku_not_in_shopify"])
    LOG.info("    - di cui con SKU presenti su Shopify: %d", stats["ok_parse_sku_in_shopify"])
    return stats


# -----------------------
# Raggruppamento immagini per SKU
# -----------------------
def group_images_by_sku(file_paths: List[str]) -> Dict[str, List[Tuple[int, str]]]:
    groups: Dict[str, List[Tuple[int, str]]] = {}
    first_choice: Dict[Tuple[str, int], str] = {}

    for path in file_paths:
        p = parse_filename_strict(path)
        if not p["ok"]:
            continue
        sku, idx = p["sku"], p["idx"]
        key = (sku, idx)

        if key not in first_choice:
            first_choice[key] = path
        else:
            existing = first_choice[key]
            base_new = os.path.splitext(os.path.basename(path))[0]
            base_old = os.path.splitext(os.path.basename(existing))[0]
            new_has_suffix = re.search(r"_(\d+)$", base_new) is not None
            old_has_suffix = re.search(r"_(\d+)$", base_old) is not None

            if idx == 1 and old_has_suffix and not new_has_suffix:
                first_choice[key] = path
                LOG.warning(
                    "Collisione su %s idx=1: preferita %s al posto di %s",
                    sku, os.path.basename(path), os.path.basename(existing)
                )
            else:
                LOG.warning(
                    "Collisione su %s idx=%d: mantengo %s, scarto %s",
                    sku, idx, os.path.basename(existing), os.path.basename(path)
                )

    for (sku, idx), path in first_choice.items():
        groups.setdefault(sku, []).append((idx, path))

    for sku in groups:
        groups[sku].sort(key=lambda x: x[0])

    return groups


# -----------------------
# Sync
# -----------------------
def sync():
    if not (SHOPIFY_STORE and SHOPIFY_TOKEN):
        raise SystemExit("Config mancante: SHOPIFY_STORE / SHOPIFY_TOKEN")
    sku_map = build_sku_index()

    if not (FTP_HOST and FTP_USER and FTP_PASSWORD):
        raise SystemExit("Config mancante: FTP_HOST / FTP_USER / FTP_PASSWORD")

    ftp = connect_ftp()
    try:
        all_files: List[str] = []

        # se usiamo sottocartelle dichiarate
        if FTP_SPLIT_DIRS:
            split_list = [p.strip() for p in FTP_SPLIT_DIRS.split(",") if p.strip()]
            LOG.info("Scansione FTP suddivisa: base=%s sottocartelle=%s", FTP_BASE_DIR or "/", split_list)

            # assicurare cartella pubblicate se serve
            if MOVE_AFTER_UPLOAD and not DRY_RUN:
                try:
                    ftp_ensure_dir(ftp, PUBLISHED_ROOT)
                    LOG.info("Cartella Pubblicate assicurata: %s", PUBLISHED_ROOT)
                except Exception as e:
                    LOG.warning("Non riesco a creare/assicurare %s: %s", PUBLISHED_ROOT, e)

            for sub in split_list:
                sub_dir = ftp_path_join(FTP_BASE_DIR or "/", sub)
                LOG.info("  → sottocartella: %s", sub_dir)
                sub_files = walk_ftp_files(ftp, sub_dir)
                LOG.info("    trovati %d file in %s", len(sub_files), sub_dir)
                all_files.extend(sub_files)
        else:
            LOG.info("Scansione FTP da %s ...", FTP_BASE_DIR or "/")

            if MOVE_AFTER_UPLOAD and not DRY_RUN:
                try:
                    ftp_ensure_dir(ftp, PUBLISHED_ROOT)
                    LOG.info("Cartella Pubblicate assicurata: %s", PUBLISHED_ROOT)
                except Exception as e:
                    LOG.warning("Non riesco a creare/assicurare %s: %s", PUBLISHED_ROOT, e)

            all_files = walk_ftp_files(ftp, FTP_BASE_DIR or "/")

        LOG.info(
            "Trovati %d file totali su FTP (escludendo '%s')",
            len(all_files),
            PUBLISHED_ROOT if MOVE_AFTER_UPLOAD else "N/A"
        )

        analyze_files(all_files, sku_map)
        groups = group_images_by_sku(all_files)
        LOG.info("Trovati %d SKU con immagini nel formato atteso", len(groups))

    finally:
        try:
            ftp.quit()
        except Exception:
            pass

    uploaded_count = 0
    skipped_unknown_sku = 0
    duplicated = 0
    errors = 0

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
                LOG.warning("Impossibile leggere immagini esistenti per %s: %s", sku, e)

            LOG.info("==> SKU %s | %d immagini da sincronizzare", sku, len(items))

            for idx, path in items:
                desired_position = idx
                alt = f"{sku}_{idx}"

                # SKIP se già presente
                if alt in existing_by_alt:
                    duplicated += 1
                    LOG.info("  - già presente (alt=%s), skip (pos prevista=%d)", alt, desired_position)
                    # non spostiamo il file in questo caso
                    continue

                try:
                    content = ftp_read_file(ftp, path)
                    attachment_b64 = base64.b64encode(content).decode("ascii")

                    create_product_image(
                        product_id=product_id,
                        attachment_b64=attachment_b64,
                        alt=alt,
                        position=desired_position,
                        variant_id=variant_id if ALSO_ATTACH_TO_VARIANT else None
                    )

                    uploaded_count += 1
                    LOG.info(
                        "  + caricato %s (pos=%d)%s",
                        alt,
                        desired_position,
                        " [DRY RUN]" if DRY_RUN else ""
                    )

                    time.sleep(0.4)

                    if MOVE_AFTER_UPLOAD and not DRY_RUN:
                        rel = path[len(FTP_BASE_DIR):] if path.startswith(FTP_BASE_DIR) else path
                        rel = rel.lstrip("/")
                        dst = ftp_path_join(FTP_BASE_DIR, FTP_PUBLISHED_DIR, rel)
                        try:
                            ftp_move(ftp, path, dst)
                            LOG.info("    ↪ spostato su %s", dst)
                        except Exception as me:
                            LOG.warning("    ⚠ impossibile spostare %s → %s: %s", path, dst, me)

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
    LOG.info("Duplicati (skip perché già caricati): %d", duplicated)
    LOG.info("SKU non trovati in Shopify: %d", skipped_unknown_sku)
    LOG.info("Errori: %d", errors)


# -----------------------
# Main
# -----------------------
if __name__ == "__main__":
    LOG.info("Script version: %s", SCRIPT_VERSION)
    LOG.info("Avvio sincronizzazione immagini FTP → Shopify (ordine SKU, SKU_2, ...)")
    LOG.info(
        "DRY_RUN=%s | ALSO_ATTACH_TO_VARIANT=%s | MOVE_AFTER_UPLOAD=%s | PUBLISHED_DIR=%s | FTP_SPLIT_DIRS=%s",
        DRY_RUN,
        ALSO_ATTACH_TO_VARIANT,
        MOVE_AFTER_UPLOAD,
        PUBLISHED_ROOT,
        FTP_SPLIT_DIRS,
    )
    sync()
