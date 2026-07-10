#!/usr/bin/env python3
from __future__ import annotations
import csv, difflib, hashlib, html, io, json, re, tarfile, time, unicodedata, zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote, urljoin
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

ROOT=Path(__file__).resolve().parent
QUEUE=ROOT/'retry_queue.csv'
OUT=ROOT/'results'
PDF_DIR=OUT/'pdf'
OUT.mkdir(exist_ok=True); PDF_DIR.mkdir(exist_ok=True)
UA='Mozilla/5.0 ENT-Open-Literature-Retry/2.0'
EMAIL='hyu273514@users.noreply.github.com'
TIMEOUT=(20,90)
S=requests.Session(); S.headers.update({'User-Agent':UA,'Accept-Language':'en-US,en;q=0.8'})
BAD_TOKENS={'clinical','practice','guideline','update','review','systematic','meta','analysis','the','and','for','with','from','using','pdf','full','text','article','study','journal','official'}

def norm(s):
    s=unicodedata.normalize('NFKC',s or '').lower(); s=re.sub(r'https?://\S+',' ',s); s=re.sub(r'[^\w\u4e00-\u9fff]+',' ',s)
    return ' '.join(s.split())

def tokens(s): return [x for x in re.findall(r'[a-z]{4,}|[\u4e00-\u9fff]{2,}',norm(s)) if x not in BAD_TOKENS]
def title_score(title,text,meta=''):
    t=norm(title); head=norm((meta+' '+text)[:18000])
    if not t or not head: return 0.0,0
    seq=difflib.SequenceMatcher(None,t,head[:max(400,len(t)*4)]).ratio(); tt=tokens(title); ht=set(tokens(head)); overlap=sum(x in ht for x in tt); ratio=overlap/max(1,min(len(tt),12))
    return max(seq,ratio),overlap

def safe(s,n=120):
    s=unicodedata.normalize('NFKC',s or ''); s=re.sub(r'[<>:"/\\|?*\x00-\x1f]+','_',s); s=re.sub(r'\s+','_',s); s=re.sub(r'_+','_',s).strip('._ ')
    return (s or 'untitled')[:n]
def sha(data): return hashlib.sha256(data).hexdigest()
def request(url, **kw):
    last=None
    for i in range(3):
        try:
            r=S.get(url,timeout=TIMEOUT,allow_redirects=True,**kw)
            if r.status_code==429: time.sleep(2+i*3); continue
            r.raise_for_status(); return r
        except Exception as e: last=e; time.sleep(1+i*2)
    raise last

def parse_pdf(data):
    if not data.lstrip().startswith(b'%PDF-'): raise ValueError('not PDF')
    reader=PdfReader(io.BytesIO(data),strict=False)
    if len(reader.pages)<1: raise ValueError('zero pages')
    text=''
    for p in reader.pages[:3]:
        try: text+='\n'+(p.extract_text() or '')
        except: pass
    meta=''
    try: meta=str(reader.metadata.title or '')
    except: pass
    return len(reader.pages),text,meta

def pdf_links_from_html(base,content):
    soup=BeautifulSoup(content,'html.parser'); out=[]
    for tag in soup.find_all('meta'):
        name=(tag.get('name') or '').lower()
        if 'pdf' in name and tag.get('content'): out.append(urljoin(base,html.unescape(tag['content'])))
    for tag in soup.find_all(['a','iframe','embed','object']):
        u=tag.get('href') or tag.get('src') or tag.get('data')
        if not u: continue
        u=urljoin(base,html.unescape(u)); label=' '.join(tag.get_text(' ',strip=True).split()).lower()
        if re.search(r'\.pdf(?:$|[?#])|/pdf(?:/|$)|pdf=|download',u,re.I) or 'pdf' in label or 'full text' in label: out.append(u)
    seen=[]
    for u in out:
        if u.startswith('http') and u not in seen: seen.append(u)
    return seen[:30]

def fetch_url_pdf(url,title,trust=False):
    r=request(url); ctype=(r.headers.get('content-type') or '').lower(); data=r.content
    if data.lstrip().startswith(b'%PDF-') or 'application/pdf' in ctype:
        pages,text,meta=parse_pdf(data); score,over=title_score(title,text,meta)
        if not trust and text.strip() and score<0.30 and over<2: raise ValueError(f'title mismatch score={score:.2f}')
        return data,pages,score,r.url
    if 'html' in ctype or data.lstrip().startswith((b'<!DOCTYPE',b'<html',b'<?xml')):
        for u in pdf_links_from_html(r.url,data[:6_000_000]):
            try:
                rr=request(u); d=rr.content; pages,text,meta=parse_pdf(d); score,over=title_score(title,text,meta)
                if text.strip() and score<0.30 and over<2: continue
                return d,pages,score,rr.url
            except Exception: continue
    raise ValueError('no matching PDF')

def europe_search(row):
    if row.get('pmid'): q=f'EXT_ID:{row["pmid"]} AND SRC:MED'
    elif row.get('doi'): q=f'DOI:"{row["doi"]}"'
    else: q=f'TITLE:"{row["title"]}"'
    data=request('https://www.ebi.ac.uk/europepmc/webservices/rest/search?format=json&pageSize=5&query='+quote(q)).json().get('resultList',{}).get('result',[])
    best=None
    for x in data:
        sc=difflib.SequenceMatcher(None,norm(row['title']),norm(x.get('title',''))).ratio()
        if row.get('pmid') and x.get('pmid')==row['pmid']: sc=1
        if row.get('doi') and norm(x.get('doi',''))==norm(row['doi']): sc=1
        if best is None or sc>best[0]: best=(sc,x)
    if best and best[0]>=0.70:
        x=best[1]; row['pmcid']=row.get('pmcid') or x.get('pmcid',''); row['pmid']=row.get('pmid') or x.get('pmid',''); row['doi']=row.get('doi') or x.get('doi','')

def pmc_oa(row):
    pmc=(row.get('pmcid') or '').upper()
    if not pmc:return None
    oa=request(f'https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id={pmc}').content; soup=BeautifulSoup(oa,'xml'); links=[x.get('href') for x in soup.find_all('link') if x.get('href')]
    for href in links:
        if href.startswith('ftp://'): href='https://'+href[6:]
        if href.endswith(('.tar.gz','.tgz')):
            try:
                blob=request(href).content
                with tarfile.open(fileobj=io.BytesIO(blob),mode='r:gz') as tf:
                    candidates=[]
                    for m in tf.getmembers():
                        if m.isfile() and m.name.lower().endswith('.pdf'):
                            f=tf.extractfile(m); d=f.read() if f else b''
                            try:
                                pages,text,meta=parse_pdf(d); sc,ov=title_score(row['title'],text,meta); candidates.append((sc,ov,len(d),d,pages,href+'#'+m.name))
                            except: pass
                    if candidates:
                        candidates.sort(reverse=True,key=lambda x:(x[0],x[1],x[2])); sc,ov,_,d,pages,src=candidates[0]
                        if sc>=0.25 or ov>=2 or not parse_pdf(d)[1].strip(): return d,pages,sc,src
            except Exception: pass
        elif '.pdf' in href.lower():
            try:return fetch_url_pdf(href,row['title'])
            except: pass
    for page in [f'https://pmc.ncbi.nlm.nih.gov/articles/{pmc}/',f'https://europepmc.org/article/MED/{pmc}']:
        try:return fetch_url_pdf(page,row['title'])
        except: pass
    return None

def oa_candidates(row):
    urls=[]; doi=(row.get('doi') or '').strip()
    if doi:
        try:
            d=request('https://api.unpaywall.org/v2/'+quote(doi,safe='')+'?email='+quote(EMAIL)).json()
            for loc in [d.get('best_oa_location'),*(d.get('oa_locations') or [])]:
                if loc:
                    for k in ('url_for_pdf','url'):
                        if loc.get(k): urls.append(loc[k])
        except: pass
        try:
            d=request('https://api.openalex.org/works/https://doi.org/'+quote(doi,safe='/()')+'?mailto='+quote(EMAIL)).json()
            for loc in [d.get('best_oa_location'),d.get('primary_location')]:
                if loc:
                    for k in ('pdf_url','landing_page_url'):
                        if loc.get(k): urls.append(loc[k])
        except: pass
        urls.append('https://doi.org/'+doi)
    if row.get('url'): urls.insert(0,row['url'])
    return list(dict.fromkeys(u for u in urls if u and u.startswith('http')))

def save(row,data,pages,score,src,method):
    spec=safe(row.get('specialties') or '未分类',30); dis=safe(row.get('diseases') or '未分类',60); folder=PDF_DIR/spec/dis; folder.mkdir(parents=True,exist_ok=True)
    year='年份不详'; m=re.search(r'\b(19|20)\d{2}\b',row.get('title',''))
    if m: year=m.group(0)
    path=folder/f'{year}_{safe(row["title"],130)}.pdf'; digest=sha(data)
    for old in PDF_DIR.rglob('*.pdf'):
        try:
            if old!=path and hashlib.sha256(old.read_bytes()).hexdigest()==digest:return {**row,'status':'重复PDF','path':str(old.relative_to(OUT)),'pages':pages,'size_bytes':len(data),'sha256':digest,'score':round(score,3),'source':src,'method':method}
        except: pass
    path.write_bytes(data); return {**row,'status':'成功','path':str(path.relative_to(OUT)),'pages':pages,'size_bytes':len(data),'sha256':digest,'score':round(score,3),'source':src,'method':method}

def process(row):
    row={k:(v or '').strip() for k,v in row.items()}; errors=[]
    try:europe_search(row)
    except Exception as e:errors.append('EuropeSearch '+repr(e)[:160])
    try:
        got=pmc_oa(row)
        if got:
            data,pages,score,src=got; return save(row,data,pages,score,src,'PMC/OA')
    except Exception as e:errors.append('PMC '+repr(e)[:180])
    for u in oa_candidates(row):
        try:
            data,pages,score,src=fetch_url_pdf(u,row['title']); return save(row,data,pages,score,src,'OA/landing')
        except Exception as e:errors.append(u[:50]+' '+repr(e)[:110])
    return {**row,'status':'失败','error':' | '.join(errors[-8:])[:1800]}

def make_parts():
    files=sorted(PDF_DIR.rglob('*.pdf')); parts=[]; current=[]; size=0; limit=45*1024*1024
    for f in files:
        if current and size+f.stat().st_size>limit:parts.append(current);current=[];size=0
        current.append(f);size+=f.stat().st_size
    if current:parts.append(current)
    for i,group in enumerate(parts,1):
        with zipfile.ZipFile(OUT/f'pdf_part_{i:03d}.zip','w',zipfile.ZIP_DEFLATED,allowZip64=True) as zz:
            for f in group:zz.write(f,f.relative_to(PDF_DIR))
    return len(parts)

def main():
    rows=list(csv.DictReader(open(QUEUE,encoding='utf-8-sig',newline='')));done=[]
    with ThreadPoolExecutor(max_workers=4) as ex:
        fut={ex.submit(process,r):r for r in rows}
        for i,f in enumerate(as_completed(fut),1):
            try:r=f.result()
            except Exception as e:r={**fut[f],'status':'失败','error':repr(e)}
            done.append(r);print(f'[{i}/{len(rows)}] {r.get("status")} {r.get("title","")[:90]}',flush=True)
    fields=sorted({k for r in done for k in r})
    for name,subset in [('retry_all.csv',done),('retry_success.csv',[r for r in done if r.get('status') in ('成功','重复PDF')]),('retry_failed.csv',[r for r in done if r.get('status')=='失败'])]:
        with open(OUT/name,'w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(subset)
    parts=make_parts();summary={'total':len(done),'success':sum(r.get('status')=='成功' for r in done),'duplicate':sum(r.get('status')=='重复PDF' for r in done),'failed':sum(r.get('status')=='失败' for r in done),'pdf_files':len(list(PDF_DIR.rglob('*.pdf'))),'zip_parts':parts};(OUT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(summary,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
