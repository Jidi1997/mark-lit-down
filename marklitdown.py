import os
import re
import json
import time
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import bibtexparser
import trafilatura
import pymupdf
import pymupdf4llm
from dotenv import load_dotenv

# Optional Gemini Integration for scanned PDF fallback
try:
    import google.generativeai as genai
    HAS_GEMINI_SDK = True
except ImportError:
    HAS_GEMINI_SDK = False

# Load environment variables (prefer local .env inside repo directory)
repo_env = Path(__file__).resolve().parent / ".env"
if repo_env.exists():
    load_dotenv(dotenv_path=repo_env)
else:
    load_dotenv()

# ================= CONFIGURATION =================
BIB_FILE_PATH = os.getenv("BIB_FILE_PATH", "./library.bib")
ZOTERO_STORAGE_PATH = os.getenv("ZOTERO_STORAGE_PATH", "./storage")
OUTPUT_MD_DIR = os.getenv("OUTPUT_MD_DIR", "./markdown_database/")
LOG_FILE = os.getenv("LOG_FILE", "./markdown_database/missing_log.txt")
MANIFEST_FILE = os.getenv("MANIFEST_FILE", "./markdown_database/manifest.json")

# Gemini Configuration for Scanned Documents
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_PATCH_SIZE = int(os.getenv("GEMINI_PATCH_SIZE", 5))

# Local Processing Workers (Apple Silicon M-series can smoothly run 6-8 threads)
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 6))
# =================================================

trafilatura_lock = threading.Lock()
print_lock = threading.Lock()
manifest_lock = threading.Lock()

os.makedirs(OUTPUT_MD_DIR, exist_ok=True)

# Initialize Gemini if key is present
if HAS_GEMINI_SDK and GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"[!] Warning: Gemini SDK configuration failed: {e}")

def safe_print(msg):
    """Thread-safe console logger."""
    with print_lock:
        print(msg)

def write_markdown_output(output_path, citekey, source, status, body):
    """Write normalized Markdown output with consistent Obsidian-compatible YAML frontmatter."""
    yaml_frontmatter = (
        f"---\n"
        f"citekey: {citekey}\n"
        f"source: {source}\n"
        f"status: {status}\n"
        f"---\n\n"
    )
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(yaml_frontmatter + body.strip() + "\n")

def append_manifest_record(record):
    """Persist processing manifest record."""
    with manifest_lock:
        manifest_path = Path(MANIFEST_FILE)
        existing = []
        if manifest_path.exists():
            try:
                existing = json.loads(manifest_path.read_text(encoding='utf-8'))
            except Exception:
                existing = []
        existing.append(record)
        manifest_path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )

def parse_zotero_file_field(file_str):
    """Parse Zotero BibTeX file field into valid local attachment paths."""
    paths = {'pdf': None, 'html': None}
    if not file_str:
        return paths
    
    file_str = file_str.replace('\\_', '_').replace('\\', '/')
    files = file_str.split(';')
    for f in files:
        f = f.strip(' {}')
        parts = f.split(':')
        potential_path = ""
        for part in parts:
            if '.pdf' in part.lower() or '.html' in part.lower():
                potential_path = part
                break
        if not potential_path:
            continue
            
        path_parts = potential_path.split('/')
        if len(path_parts) >= 2:
            folder_and_file = os.path.join(path_parts[-2], path_parts[-1])
            final_path = os.path.join(ZOTERO_STORAGE_PATH, folder_and_file)
        else:
            final_path = os.path.join(ZOTERO_STORAGE_PATH, potential_path)

        if final_path.lower().endswith('.pdf'):
            paths['pdf'] = final_path
        elif final_path.lower().endswith('.html'):
            paths['html'] = final_path
    return paths

def is_born_digital_pdf(doc, sample_pages=5, min_chars_per_page=80):
    """
    Probe the first few pages of a PDF to check for a native digital text layer.
    Returns True for born-digital PDFs, False for pure image/scanned documents.
    """
    total_pages = len(doc)
    pages_to_check = min(total_pages, sample_pages)
    if pages_to_check == 0:
        return False
    
    total_chars = 0
    for i in range(pages_to_check):
        total_chars += len(doc[i].get_text("text").strip())
        
    avg_chars = total_chars / pages_to_check
    return avg_chars >= min_chars_per_page

def clean_academic_text(text):
    """Strip common academic noise headers and disclaimers."""
    cleaned_lines = []
    noise_patterns = [
        r"^Received\s+[A-Za-z]+\s+\d{1,2}.*", r".*editorial decision.*", 
        r"^Published:?", r"^(The )?Review of Financial Studies.*", 
        r"^Volume\s+\d+.*Issue\s+\d+", r"^https://doi\.org/.*", 
        r"^\(?[cC]\)?\s*The Author.*", r".*All rights reserved.*", 
        r".*For Permissions.*", r"^You do not currently have access.*", 
        r"^Issue Section:", r"^Collection:"
    ]
    for line in text.split('\n'):
        line_stripped = line.strip()
        if not line_stripped:
            continue
        is_noise = any(re.search(p, line_stripped, re.IGNORECASE) for p in noise_patterns)
        if not is_noise:
            cleaned_lines.append(line_stripped)
    return '\n\n'.join(cleaned_lines)

def convert_html_to_md(html_path):
    """Extract HTML body text using Trafilatura."""
    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
            
        with trafilatura_lock:
            extracted_text = trafilatura.extract(html_content, include_comments=False, include_tables=True)
            
        if extracted_text:
            pure_text = clean_academic_text(extracted_text)
            return pure_text if pure_text else None
        return None
    except Exception:
        return None

def convert_pdf_with_pymupdf(pdf_path, output_dir, citekey):
    """Tier 1: High-speed local conversion for born-digital PDFs via PyMuPDF4LLM."""
    try:
        md_text = pymupdf4llm.to_markdown(pdf_path)
        if not md_text or not md_text.strip():
            return False
        final_md_path = os.path.join(output_dir, f"{citekey}.md")
        write_markdown_output(final_md_path, citekey, "pdf pymupdf", "complete", md_text)
        return True
    except Exception as e:
        safe_print(f"  [{citekey}] ⚠️ PyMuPDF extraction exception: {e}")
        return False

def convert_scanned_pdf_with_gemini(doc, output_dir, citekey):
    """
    Tier 2: Adaptive-Patching Vision OCR for scanned PDFs via Gemini 2.5 Flash.
    - Pages <= 10: Single pass in-memory PDF byte stream.
    - Pages > 10: Sliced in chunks of GEMINI_PATCH_SIZE (e.g. 5 pages) to prevent 8k output token cut-off.
    """
    if not (HAS_GEMINI_SDK and GEMINI_API_KEY):
        safe_print(f"  [{citekey}] ⚠️ GEMINI_API_KEY missing. Cannot perform cloud OCR on scanned PDF.")
        return False

    prompt = (
        "Transcribe this academic document slice into standard GitHub Flavored Markdown. "
        "Preserve section headers (#, ##), maintain Markdown tables, and format mathematical formulas "
        "into LaTeX ($...$ and $$...$$). Output pure markdown without conversational preamble."
    )
    
    total_pages = len(doc)
    model = genai.GenerativeModel(GEMINI_MODEL)
    collected_markdown = []

    try:
        if total_pages <= 10:
            safe_print(f"  [{citekey}] 🌐 Invoking Gemini {GEMINI_MODEL} (Single-Pass: {total_pages} pages)...")
            pdf_bytes = doc.write()
            resp = model.generate_content([
                {'mime_type': 'application/pdf', 'data': pdf_bytes},
                prompt
            ])
            if resp.text:
                collected_markdown.append(resp.text.strip())
        else:
            safe_print(f"  [{citekey}] 🌐 Invoking Gemini {GEMINI_MODEL} (Adaptive Patching: {total_pages} pages in {GEMINI_PATCH_SIZE}-page chunks)...")
            for start_idx in range(0, total_pages, GEMINI_PATCH_SIZE):
                end_idx = min(start_idx + GEMINI_PATCH_SIZE - 1, total_pages - 1)
                
                slice_doc = pymupdf.open()
                slice_doc.insert_pdf(doc, from_page=start_idx, to_page=end_idx)
                slice_bytes = slice_doc.write()
                slice_doc.close()

                safe_print(f"    └── [{citekey}] Processing slice pp. {start_idx+1}-{end_idx+1}...")
                resp = model.generate_content([
                    {'mime_type': 'application/pdf', 'data': slice_bytes},
                    prompt
                ])
                if resp.text:
                    collected_markdown.append(resp.text.strip())
                time.sleep(1.0)  # Rate limit courtesy spacing

        if collected_markdown:
            full_md = "\n\n".join(collected_markdown)
            final_md_path = os.path.join(output_dir, f"{citekey}.md")
            write_markdown_output(final_md_path, citekey, "pdf gemini_ocr", "complete", full_md)
            return True
        return False
    except Exception as e:
        safe_print(f"  [{citekey}] ⚠️ Gemini OCR exception: {e}")
        return False

def process_single_entry(entry):
    """Core logic routing for a single reference entry in the Zotero library."""
    citekey = entry.get('ID')
    final_md_path = os.path.join(OUTPUT_MD_DIR, f"{citekey}.md")
    existing_source = None
    
    # 1. Identity Check: Inspect existing frontmatter
    if os.path.exists(final_md_path):
        try:
            with open(final_md_path, 'r', encoding='utf-8') as f:
                head_content = f.read(250)
                source_match = re.search(r"^source:\s*(.+)$", head_content, re.MULTILINE)
                if source_match:
                    existing_source = source_match.group(1).strip()
        except Exception:
            pass

    # Skip files already processed by full text engines
    if existing_source in ["pdf ocr", "mistral_ocr", "pdf pymupdf", "pdf gemini_ocr"]:
        safe_print(f"[{citekey}] ⏭️ Intercepted: Valid {existing_source} found. Skipping.")
        return {
            "citekey": citekey,
            "result": "skipped",
            "source": existing_source,
            "output_file": final_md_path,
        }

    files_info = parse_zotero_file_field(entry.get('file', ''))
    
    # 2. PDF Processing: Two-Tier Adaptive Engine
    if files_info['pdf'] and os.path.exists(files_info['pdf']):
        pdf_path = files_info['pdf']
        try:
            doc = pymupdf.open(pdf_path)
            is_digital = is_born_digital_pdf(doc)
            
            if is_digital:
                safe_print(f"[{citekey}] 🚀 Born-Digital PDF. Running Tier 1 (PyMuPDF4LLM)...")
                doc.close()
                if convert_pdf_with_pymupdf(pdf_path, OUTPUT_MD_DIR, citekey):
                    safe_print(f"  └── [{citekey}] 🎉 PyMuPDF extraction finalized!")
                    return {
                        "citekey": citekey,
                        "result": "success",
                        "source": "pdf pymupdf",
                        "output_file": final_md_path,
                    }
            else:
                safe_print(f"[{citekey}] 📸 Image-Only/Scanned PDF detected ({len(doc)} pages). Running Tier 2 (Gemini Flash Adaptive Patching)...")
                gemini_success = convert_scanned_pdf_with_gemini(doc, OUTPUT_MD_DIR, citekey)
                doc.close()
                if gemini_success:
                    safe_print(f"  └── [{citekey}] 🎉 Gemini OCR finalized!")
                    return {
                        "citekey": citekey,
                        "result": "success",
                        "source": "pdf gemini_ocr",
                        "output_file": final_md_path,
                    }
        except Exception as e:
            safe_print(f"  [{citekey}] ⚠️ PDF parsing error: {e}")

    # 3. HTML Processing Fallback
    if files_info['html'] and os.path.exists(files_info['html']):
        if existing_source == "html":
            return {
                "citekey": citekey,
                "result": "skipped",
                "source": "html",
                "output_file": final_md_path,
            }
        safe_print(f"[{citekey}] 📄 HTML located. Initiating lightweight local extraction...")
        html_text = convert_html_to_md(files_info['html'])
        if html_text:
            write_markdown_output(final_md_path, citekey, "html", "complete", html_text)
            return {
                "citekey": citekey,
                "result": "success",
                "source": "html",
                "output_file": final_md_path,
            }

    # 4. Abstract Fallback
    if existing_source == "abstract":
        return {
            "citekey": citekey,
            "result": "skipped",
            "source": "abstract",
            "output_file": final_md_path,
        }
        
    abstract = entry.get('abstract', '').strip()
    if abstract:
        safe_print(f"[{citekey}] 📝 Only abstract found. Generating structural markdown file...")
        write_markdown_output(final_md_path, citekey, "abstract", "partial", f"# Abstract\n\n{abstract}")
        return {
            "citekey": citekey,
            "result": "success",
            "source": "abstract",
            "output_file": final_md_path,
        }

    # 5. Missing Data Terminal Log
    safe_print(f"[{citekey}] ❌ No usable data. Logging to missing entries.")
    return {
        "citekey": citekey,
        "result": "missing",
        "source": "missing",
        "output_file": None,
    }

def main():
    safe_print(f"\n=======================================================")
    safe_print(f"Initializing Zotero Knowledge Base Engine (Two-Tier Hybrid)")
    safe_print(f"Tier 1: PyMuPDF4LLM (Local) | Tier 2: Gemini 2.5 Flash (Adaptive)")
    safe_print(f"Workers: {MAX_WORKERS} | Output: {OUTPUT_MD_DIR}")
    safe_print(f"=======================================================\n")
    
    manifest_path = Path(MANIFEST_FILE)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("[]", encoding='utf-8')

    with open(BIB_FILE_PATH, 'r', encoding='utf-8') as bibtex_file:
        bib_database = bibtexparser.load(bibtex_file)

    missing_entries = []
    success_count = 0
    skipped_count = 0
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_single_entry, entry) for entry in bib_database.entries]
        
        for future in as_completed(futures):
            result = future.result()
            append_manifest_record(result)
            if result["result"] == "missing":
                missing_entries.append(result["citekey"])
            elif result["result"] == "success":
                success_count += 1
            elif result["result"] == "skipped":
                skipped_count += 1

    if missing_entries:
        with open(LOG_FILE, 'w', encoding='utf-8') as f:
            f.write("The following CiteKeys are missing PDF, HTML, and abstract data:\n")
            for mk in missing_entries:
                f.write(f"- {mk}\n")
        
    safe_print(f"\n✅ Batch processing complete!")
    safe_print(f"   Success (New/Updated): {success_count}")
    safe_print(f"   Skipped (Already Complete): {skipped_count}")
    safe_print(f"   Missing data: {len(missing_entries)} records written to {LOG_FILE}\n")

if __name__ == "__main__":
    main()
