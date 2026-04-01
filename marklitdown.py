import os
import re
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

# Concurrent execution threads: M4 chips can handle many, but respect API rate limits. 5-10 is recommended.
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 6))
# =================================================

# 🎯 Thread Safety: trafilatura (lxml) is not fully thread-safe on macOS.
trafilatura_lock = threading.Lock()

os.makedirs(OUTPUT_MD_DIR, exist_ok=True)

# Create a thread lock to prevent terminal output interleaving from concurrent threads.
print_lock = threading.Lock()

def safe_print(msg):
    """Thread-safe terminal output."""
    with print_lock:
        print(msg)

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
            with open(output_path, 'w', encoding='utf-8') as f: f.write(pure_text)
            return True
        return False
    except Exception:
        return False

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
        
        # 🎯 Core Mechanism: Inject Native Obsidian YAML frontmatter
        yaml_frontmatter = f"---\ncitekey: {citekey}\nsource: mistral_ocr\nstatus: complete\n---\n\n"
        final_content = yaml_frontmatter + final_markdown
        
        with open(final_md_path, 'w', encoding='utf-8') as f: f.write(final_content)
        return True
    except Exception as e:
        safe_print(f"  [{citekey}] ⚠️ Mistral API Exception: {e}")
        return False

def process_single_entry(entry):
    """Core processing unit invoked by multiple threads: handles logic routing for a single reference."""
    citekey = entry.get('ID')
    final_md_path = os.path.join(OUTPUT_MD_DIR, f"{citekey}.md")
    already_ocred = False
    
    # 1. Identity Check: Verify YAML frontmatter presence
    if os.path.exists(final_md_path):
        try:
            with open(final_md_path, 'r', encoding='utf-8') as f:
                head_content = f.read(200)
                if "source: mistral_ocr" in head_content:
                    already_ocred = True
        except Exception:
            pass

    files_info = parse_zotero_file_field(entry.get('file', ''))
    
    # 2. Priority A: Process PDF
    if files_info['pdf'] and os.path.exists(files_info['pdf']):
        if already_ocred:
            safe_print(f"[{citekey}] ⏭️ Intercepted: Valid Mistral frontmatter found. Skipping processing to conserve API quota.")
            return citekey, "skipped"
        else:
            safe_print(f"[{citekey}] 🚀 PDF located. Initiating Mistral OCR cloud conversion...")
            if convert_pdf_with_api(files_info['pdf'], OUTPUT_MD_DIR, citekey):
                safe_print(f"  └── [{citekey}] 🎉 Cloud parsing finalized!")
                return citekey, "success_pdf"

    # 3. Priority B: Do not downgrade to HTML if a perfect OCR PDF exists.
    if already_ocred:
        return citekey, "skipped"

    # 4. Priority C: Process HTML
    if files_info['html'] and os.path.exists(files_info['html']):
        safe_print(f"[{citekey}] 📄 HTML located. Initiating lightweight local extraction...")
        if convert_html_to_md(files_info['html'], final_md_path):
            return citekey, "success_html"

    # 5. Priority D: Fallback to abstract extraction
    abstract = entry.get('abstract', '').strip()
    if abstract:
        safe_print(f"[{citekey}] 📝 Only abstract found. Generating structural markdown file...")
        with open(final_md_path, 'w', encoding='utf-8') as f:
            f.write(f"# Abstract\n\n{abstract}")
        return citekey, "success_abstract"

    # 6. Priority E: No data available. Log the missing entry.
    safe_print(f"[{citekey}] ❌ No usable data. Logging to missing entries.")
    return citekey, "missing"

def main():
    safe_print(f"\n==============================================")
    safe_print(f"Initializing Zotero Knowledge Base Engine (Concurrent Workers: {MAX_WORKERS})")
    safe_print(f"==============================================\n")
    
    with open(BIB_FILE_PATH, 'r', encoding='utf-8') as bibtex_file:
        bib_database = bibtexparser.load(bibtex_file)

    missing_entries = []
    
    # 🎯 Core Mechanism: Initialize thread pool for concurrent reference processing
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Dispatch asynchronous tasks to the thread pool and collect Future objects
        futures = [executor.submit(process_single_entry, entry) for entry in bib_database.entries]
        
        # Collect processing results in real-time
        for future in as_completed(futures):
            citekey, status = future.result()
            if status == "missing":
                missing_entries.append(citekey)

    # Compile final missing log
    if missing_entries:
        with open(LOG_FILE, 'w', encoding='utf-8') as f:
            f.write("The following CiteKeys are missing PDF, HTML, and abstract data:\n")
            for mk in missing_entries:
                f.write(f"- {mk}\n")
        
    safe_print(f"\n✅ Batch processing complete! {len(missing_entries)} missing records have been written to missing_log.txt\n")

if __name__ == "__main__":
    main()
