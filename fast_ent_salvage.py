import csv, hashlib, io, json, os, pathlib, re, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3
from pypdf import PdfReader

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
EMAIL = "alice246991@gmail.com"
OUT = pathlib.Path("fast_salvage_results")
PDFROOT = OUT / "pdf"
PDFROOT.mkdir(parents=True, exist_ok=True)


def safe(value, limit=150):
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(value or ""))
    return re.sub(r"\s+", "_", value).strip("._ ")[:limit] or "untitled"


def words(value):
    stop = {"with", "from", "that", "this", "into", "after", "using", "study", "review", "clinical", "patients", "guideline"}
    return {w for w in re.findall(r"[a-z0-9]+", str(value).lower()) if len(w) > 3 and w not in stop}


def validate_pdf(data, title):
    if not data.lstrip().startswith(b"%PDF-"):
        return None, "not_pdf"
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        pages = len(reader.pages)
        text = " ".join((reader.pages[i].extract_text() or "") for i in range(min(3, pages)))
        score = len(words(title) & words(text)) / max(1, len(words(title)))
        if text and score < 0.20:
            return None, f"title_mismatch:{score:.2f}"
        return (pages, len(data), hashlib.sha256(data).hexdigest(), score), ""
    except Exception as exc:
        return None, f"pdf_error:{type(exc).__name__}:{str(exc)[:100]}"


def fetch_json(session, url):
    response = session.get(url, timeout=(7, 18), verify=False)
    response.raise_for_status()
    return response.json()


def precise_candidates(row):
    candidates = [("publisher", u) for u in row.get("publisher_urls", []) if u]
    pmcid = row.get("pmcid", "")
    number = pmcid.replace("PMC", "")
    for href in row.get("hrefs", []):
        encoded = urllib.parse.quote(href, safe="._-")
        candidates.extend([
            ("pmc_bin", f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/bin/{encoded}"),
            ("pmc_instance", f"https://pmc.ncbi.nlm.nih.gov/articles/instance/{number}/bin/{encoded}"),
            ("europepmc_bin", f"https://europepmc.org/articles/{pmcid}/bin/{encoded}"),
        ])
    candidates.extend([
        ("europepmc_render", f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmcid}&blobtype=pdf"),
        ("europepmc_api", f"https://europepmc.org/api/getPdf?pmcid={pmcid}"),
    ])
    return candidates


def repository_candidates(session, row, errors):
    candidates = []
    doi, pmid, pmcid = row.get("doi", ""), row.get("pmid", ""), row.get("pmcid", "")
    identifiers = ([f"DOI:{doi}"] if doi else []) + ([f"PMID:{pmid}"] if pmid else []) + ([f"PMCID:{pmcid}"] if pmcid else [])
    for ident in identifiers:
        try:
            data = fetch_json(session, "https://api.semanticscholar.org/graph/v1/paper/" + urllib.parse.quote(ident, safe=":") + "?fields=openAccessPdf")
            url = (data.get("openAccessPdf") or {}).get("url")
            if url:
                candidates.append(("semantic_scholar", url))
            break
        except Exception as exc:
            errors.append(f"S2:{type(exc).__name__}")
    if doi:
        try:
            data = fetch_json(session, f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi, safe='')}?email={EMAIL}")
            locations = [data.get("best_oa_location"), data.get("first_oa_location")] + (data.get("oa_locations") or [])
            for loc in locations:
                if loc and loc.get("url_for_pdf"):
                    candidates.append(("unpaywall", loc["url_for_pdf"]))
        except Exception as exc:
            errors.append(f"Unpaywall:{type(exc).__name__}")
    identifiers = ([f"https://doi.org/{doi}"] if doi else []) + ([f"https://pubmed.ncbi.nlm.nih.gov/{pmid}"] if pmid else [])
    for ident in identifiers:
        try:
            data = fetch_json(session, "https://api.openalex.org/works/" + urllib.parse.quote(ident, safe="") + f"?mailto={EMAIL}")
            locations = [data.get("best_oa_location"), data.get("primary_location")] + (data.get("locations") or [])
            for loc in locations:
                if loc and loc.get("pdf_url"):
                    candidates.append(("openalex", loc["pdf_url"]))
            break
        except Exception as exc:
            errors.append(f"OpenAlex:{type(exc).__name__}")
    if doi:
        try:
            data = fetch_json(session, "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe=""))
            for link in data.get("message", {}).get("link", []) or []:
                if link.get("URL"):
                    candidates.append(("crossref", link["URL"]))
        except Exception as exc:
            errors.append(f"Crossref:{type(exc).__name__}")
    return candidates


def process(row, mode):
    session = requests.Session()
    session.headers.update({"User-Agent": f"ENT-Literature-Archive/1.0 (mailto:{EMAIL})", "Accept": "application/pdf,*/*"})
    title = row.get("title") or row.get("titles") or ""
    key = row.get("key") or row.get("pmcid") or row.get("doi") or safe(title, 50)
    errors = []
    candidates = precise_candidates(row) if mode == "precise" else repository_candidates(session, row, errors)
    seen = set()
    result = {**row, "status": "failed", "method": "", "source_url": "", "path": "", "pages": "", "size_bytes": "", "sha256": "", "match_score": "", "error": ""}
    for method, url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            response = session.get(url, timeout=(7, 22), allow_redirects=True, verify=False)
            info, error = validate_pdf(response.content, title)
            if info:
                path = PDFROOT / f"{safe(key, 60)}_{safe(title)}_{method}.pdf"
                path.write_bytes(response.content)
                result.update(status="success", method=method, source_url=response.url, path=str(path), pages=info[0], size_bytes=info[1], sha256=info[2], match_score=round(info[3], 3), error="")
                return result
            errors.append(f"{method}:{response.status_code}:{response.headers.get('content-type','')}:{error}")
        except Exception as exc:
            errors.append(f"{method}:{type(exc).__name__}:{str(exc)[:100]}")
    result["error"] = " | ".join(errors)[:12000]
    return result


def main():
    if pathlib.Path("precise_fix_queue.json").exists():
        mode, rows = "precise", json.load(open("precise_fix_queue.json", encoding="utf-8"))
    elif pathlib.Path("repo_discovery_queue.json").exists():
        mode, rows = "repository", json.load(open("repo_discovery_queue.json", encoding="utf-8"))
    else:
        raise SystemExit("No queue found")
    results = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(process, row, mode): row for row in rows}
        for index, future in enumerate(as_completed(futures), 1):
            result = future.result()
            results.append(result)
            print(f"[{index}/{len(rows)}] {result['status']} {result.get('pmcid') or result.get('key')} {result.get('method','')} {result.get('pages','')}", flush=True)
    fields = []
    for result in results:
        for key in result:
            if key not in fields:
                fields.append(key)
    with open(OUT / "report.csv", "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader(); writer.writerows(results)
    summary = {"mode": mode, "total": len(results), "success": sum(r["status"] == "success" for r in results), "failed": sum(r["status"] != "success" for r in results), "pages": sum(int(r.get("pages") or 0) for r in results), "bytes": sum(int(r.get("size_bytes") or 0) for r in results)}
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
