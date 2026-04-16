import os
import re
import json
import bibtexparser
import trafilatura
from pathlib import Path
from mistralai.client import Mistral
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()

# ================= CONFIGURATION =================
# Set these via environment variables (e.g., export MISTRAL_API_KEY=xxx)
# or edit the defaults below for local use.
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")

BIB_FILE_PATH = os.getenv("BIB_FILE_PATH", "./library.bib")
ZOTERO_STORAGE_PATH = os.getenv("ZOTERO_STORAGE_PATH", "./storage")
OUTPUT_MD_DIR = os.getenv("OUTPUT_MD_DIR", "./markdown_database/")
LOG_FILE = os.getenv("LOG_FILE", "./markdown_database/missing_log.txt")
MANIFEST_FILE = os.getenv("MANIFEST_FILE", "./markdown_database/manifest.json")

# Concurrent execution threads: M4 chips can handle many, but respect API rate limits. 5-10 is recommended.
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 6))
# =================================================

# 🎯 Thread Safety: trafilatura (lxml) is not fully thread-safe on macOS.
trafilatura_lock = threading.Lock()

os.makedirs(OUTPUT_MD_DIR, exist_ok=True)

# Create a thread lock to prevent terminal output interleaving from concurrent threads.
print_lock = threading.Lock()
manifest_lock = threading.Lock()

def safe_print(msg):
    """Thread-safe terminal output."""
    with print_lock:
        print(msg)

def write_markdown_output(output_path, citekey, source, status, body):
    """Write normalized Markdown output with consistent YAML frontmatter."""
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
    """Persist a normalized processing manifest for downstream tooling."""
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
    """Intelligently parse the 'file' field in Zotero BibTeX exports."""
    paths = {'pdf': None, 'html': None}
    if not file_str: return paths
    
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
        if not potential_path: continue
            
        path_parts = potential_path.split('/')
        if len(path_parts) >= 2:
            folder_and_file = os.path.join(path_parts[-2], path_parts[-1])
            final_path = os.path.join(ZOTERO_STORAGE_PATH, folder_and_file)
        else:
            final_path = os.path.join(ZOTERO_STORAGE_PATH, potential_path)

        if final_path.lower().endswith('.pdf'): paths['pdf'] = final_path
        elif final_path.lower().endswith('.html'): paths['html'] = final_path
    return paths

def clean_academic_text(text):
    """Clean noise metadata extracted from academic webpages."""
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
        if not line_stripped: continue
        is_noise = any(re.search(p, line_stripped, re.IGNORECASE) for p in noise_patterns)
        if not is_noise: cleaned_lines.append(line_stripped)
    return '\n\n'.join(cleaned_lines)

def convert_html_to_md(html_path, output_path):
    """Intelligently extract HTML body text using Trafilatura."""
    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
            
        # 🎯 Critical Fix: Wrap in Lock to prevent macOS malloc crash in lxml
        with trafilatura_lock:
            extracted_text = trafilatura.extract(html_content, include_comments=False, include_tables=True)
            
        if extracted_text:
            pure_text = clean_academic_text(extracted_text)
            return pure_text if pure_text else None
        return None
    except Exception:
        return None

def convert_pdf_with_api(pdf_path, output_dir, citekey):
    """Invoke Mistral OCR API and inject Obsidian-native YAML frontmatter."""
    try:
        client = Mistral(api_key=MISTRAL_API_KEY)
        pdf_file = Path(pdf_path)
        
        uploaded_file = client.files.upload(
            file={"file_name": pdf_file.name, "content": pdf_file.read_bytes()},
            purpose="ocr"
        )
        signed_url = client.files.get_signed_url(file_id=uploaded_file.id, expiry=1)
        pdf_response = client.ocr.process(
            document={"type": "document_url", "document_url": signed_url.url},
            model="mistral-ocr-latest", include_image_base64=False
        )
        
        markdowns = [page.markdown for page in pdf_response.pages]
        final_markdown = "\n\n".join(markdowns)
        final_md_path = os.path.join(output_dir, f"{citekey}.md")

        write_markdown_output(final_md_path, citekey, "pdf ocr", "complete", final_markdown)
        return True
    except Exception as e:
        safe_print(f"  [{citekey}] ⚠️ Mistral API Exception: {e}")
        return False

def process_single_entry(entry):
    """Core processing unit invoked by multiple threads: handles logic routing for a single reference."""
    citekey = entry.get('ID')
    final_md_path = os.path.join(OUTPUT_MD_DIR, f"{citekey}.md")
    existing_source = None
    
    # 1. Identity Check: Verify YAML frontmatter presence
    if os.path.exists(final_md_path):
        try:
            with open(final_md_path, 'r', encoding='utf-8') as f:
                head_content = f.read(200)
                source_match = re.search(r"^source:\s*(.+)$", head_content, re.MULTILINE)
                if source_match:
                    existing_source = source_match.group(1).strip()
        except Exception:
            pass

    files_info = parse_zotero_file_field(entry.get('file', ''))
    
    # 2. Priority A: Process PDF
    if files_info['pdf'] and os.path.exists(files_info['pdf']):
        if existing_source == "pdf ocr":
            safe_print(f"[{citekey}] ⏭️ Intercepted: Valid Mistral frontmatter found. Skipping processing to conserve API quota.")
            return {
                "citekey": citekey,
                "result": "skipped",
                "source": existing_source,
                "output_file": final_md_path,
            }
        else:
            safe_print(f"[{citekey}] 🚀 PDF located. Initiating Mistral OCR cloud conversion...")
            if convert_pdf_with_api(files_info['pdf'], OUTPUT_MD_DIR, citekey):
                safe_print(f"  └── [{citekey}] 🎉 Cloud parsing finalized!")
                return {
                    "citekey": citekey,
                    "result": "success",
                    "source": "pdf ocr",
                    "output_file": final_md_path,
                }

    # 3. Priority B: Do not downgrade to HTML if a perfect OCR PDF exists.
    if existing_source == "pdf ocr":
        return {
            "citekey": citekey,
            "result": "skipped",
            "source": existing_source,
            "output_file": final_md_path,
        }

    # 4. Priority C: Process HTML
    if files_info['html'] and os.path.exists(files_info['html']):
        safe_print(f"[{citekey}] 📄 HTML located. Initiating lightweight local extraction...")
        html_text = convert_html_to_md(files_info['html'], final_md_path)
        if html_text:
            write_markdown_output(final_md_path, citekey, "html", "complete", html_text)
            return {
                "citekey": citekey,
                "result": "success",
                "source": "html",
                "output_file": final_md_path,
            }

    # 5. Priority D: Fallback to abstract extraction
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

    # 6. Priority E: No data available. Log the missing entry.
    safe_print(f"[{citekey}] ❌ No usable data. Logging to missing entries.")
    return {
        "citekey": citekey,
        "result": "missing",
        "source": "missing",
        "output_file": None,
    }

def main():
    safe_print(f"\n==============================================")
    safe_print(f"Initializing Zotero Knowledge Base Engine (Concurrent Workers: {MAX_WORKERS})")
    safe_print(f"==============================================\n")
    
    manifest_path = Path(MANIFEST_FILE)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("[]", encoding='utf-8')

    with open(BIB_FILE_PATH, 'r', encoding='utf-8') as bibtex_file:
        bib_database = bibtexparser.load(bibtex_file)

    missing_entries = []
    
    # 🎯 Core Mechanism: Initialize thread pool for concurrent reference processing
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Dispatch asynchronous tasks to the thread pool and collect Future objects
        futures = [executor.submit(process_single_entry, entry) for entry in bib_database.entries]
        
        # Collect processing results in real-time
        for future in as_completed(futures):
            result = future.result()
            append_manifest_record(result)
            if result["result"] == "missing":
                missing_entries.append(result["citekey"])

    # Compile final missing log
    if missing_entries:
        with open(LOG_FILE, 'w', encoding='utf-8') as f:
            f.write("The following CiteKeys are missing PDF, HTML, and abstract data:\n")
            for mk in missing_entries:
                f.write(f"- {mk}\n")
        
    safe_print(f"\n✅ Batch processing complete! {len(missing_entries)} missing records have been written to missing_log.txt\n")

if __name__ == "__main__":
    main()
