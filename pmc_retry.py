#!/usr/bin/env python3
import csv, hashlib, io, json, tarfile, time, zipfile
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

PMCIDS=['PMC10046729','PMC10105630','PMC10116981','PMC10132484','PMC10136624','PMC10219546','PMC10231305','PMC10302809','PMC10331632','PMC10471902','PMC10493630','PMC10597714','PMC10789606','PMC11098544','PMC11455860','PMC11464026','PMC11477254','PMC11564285','PMC11968381','PMC12640654','PMC2699173','PMC2846828','PMC2848314','PMC3250580','PMC3312594','PMC3360444','PMC3618744','PMC3652961','PMC3975551','PMC4221774','PMC4647018','PMC4881317','PMC4977739','PMC5063726','PMC5337595','PMC5550312','PMC5554813','PMC5787654','PMC5954889','PMC6081282','PMC6265171','PMC6331292','PMC6331298','PMC6374080','PMC6374094','PMC6486127','PMC6750779','PMC6929568','PMC6942543','PMC6956409','PMC7040130','PMC7108032','PMC7118881','PMC7154615','PMC7224431','PMC7335535','PMC7669319','PMC7752060','PMC7947742','PMC8002749','PMC8134171','PMC8148678','PMC8314660','PMC8356106','PMC8726361','PMC8728675','PMC8933266','PMC9122763','PMC9445910']
OUT=Path('results_pmc');PDF=OUT/'pdf';PDF.mkdir(parents=True,exist_ok=True)
S=requests.Session();S.headers['User-Agent']='Mozilla/5.0 ENT-PMC-Retry/1.0'

def get(url):
    last=None
    for i in range(4):
        try:
            r=S.get(url,timeout=(20,120),allow_redirects=True)
            if r.status_code==429:time.sleep(3+i*3);continue
            r.raise_for_status();return r
        except Exception as e:last=e;time.sleep(1+i*2)
    raise last

def valid_pdf(data):
    if not data.lstrip().startswith(b'%PDF-'):return None
    try:
        reader=PdfReader(io.BytesIO(data),strict=False)
        return len(reader.pages) if len(reader.pages)>0 else None
    except:return None

def meta(pmcid):
    try:
        q=f'https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=PMCID:{pmcid}&format=json&pageSize=1'
        x=get(q).json().get('resultList',{}).get('result',[{}])[0]
        return {'title':x.get('title',''),'doi':x.get('doi',''),'pmid':x.get('pmid','')}
    except:return {'title':'','doi':'','pmid':''}

def download_one(pmcid):
    m=meta(pmcid);errors=[]
    try:
        xml=get(f'https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id={pmcid}').content
        soup=BeautifulSoup(xml,'xml')
        links=[x.get('href') for x in soup.find_all('link') if x.get('href')]
        for href in links:
            if href.startswith('ftp://'):href='https://'+href[6:]
            try:
                if href.endswith(('.tar.gz','.tgz')):
                    blob=get(href).content
                    with tarfile.open(fileobj=io.BytesIO(blob),mode='r:gz') as tf:
                        pdfs=[]
                        for member in tf.getmembers():
                            if member.isfile() and member.name.lower().endswith('.pdf'):
                                f=tf.extractfile(member);data=f.read() if f else b'';pages=valid_pdf(data)
                                if pages:pdfs.append((len(data),data,pages,member.name))
                        if pdfs:
                            _,data,pages,name=max(pdfs,key=lambda z:z[0]);path=PDF/f'{pmcid}.pdf';path.write_bytes(data)
                            return {**m,'pmcid':pmcid,'status':'成功','pages':pages,'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'source':href+'#'+name,'path':str(path)}
                elif '.pdf' in href.lower():
                    data=get(href).content;pages=valid_pdf(data)
                    if pages:
                        path=PDF/f'{pmcid}.pdf';path.write_bytes(data)
                        return {**m,'pmcid':pmcid,'status':'成功','pages':pages,'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'source':href,'path':str(path)}
            except Exception as e:errors.append(repr(e)[:180])
    except Exception as e:errors.append(repr(e)[:180])
    # fallback: parse canonical PMC HTML for citation_pdf_url/links
    try:
        page=get(f'https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/')
        soup=BeautifulSoup(page.content,'html.parser');urls=[]
        for tag in soup.find_all('meta'):
            if 'pdf' in (tag.get('name') or '').lower() and tag.get('content'):urls.append(requests.compat.urljoin(page.url,tag['content']))
        for a in soup.find_all('a',href=True):
            u=requests.compat.urljoin(page.url,a['href'])
            if '.pdf' in u.lower() or '/pdf/' in u.lower():urls.append(u)
        for u in dict.fromkeys(urls):
            try:
                data=get(u).content;pages=valid_pdf(data)
                if pages:
                    path=PDF/f'{pmcid}.pdf';path.write_bytes(data)
                    return {**m,'pmcid':pmcid,'status':'成功','pages':pages,'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'source':u,'path':str(path)}
            except Exception as e:errors.append(repr(e)[:180])
    except Exception as e:errors.append(repr(e)[:180])
    return {**m,'pmcid':pmcid,'status':'失败','error':' | '.join(errors[-6:])}

def main():
    rows=[]
    for i,p in enumerate(PMCIDS,1):
        r=download_one(p);rows.append(r);print(f'[{i}/{len(PMCIDS)}] {r["status"]} {p} {r.get("title","")[:70]}',flush=True)
    fields=sorted({k for r in rows for k in r})
    with open(OUT/'report.csv','w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
    # zip into <=45 MB groups
    files=sorted(PDF.glob('*.pdf'));groups=[];cur=[];size=0
    for p in files:
        if cur and size+p.stat().st_size>45*1024*1024:groups.append(cur);cur=[];size=0
        cur.append(p);size+=p.stat().st_size
    if cur:groups.append(cur)
    for i,g in enumerate(groups,1):
        with zipfile.ZipFile(OUT/f'pmc_part_{i:03d}.zip','w',zipfile.ZIP_DEFLATED) as z:
            for p in g:z.write(p,p.name)
    summary={'total':len(rows),'success':sum(r['status']=='成功' for r in rows),'failed':sum(r['status']=='失败' for r in rows),'parts':len(groups)}
    (OUT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8');print(summary)
if __name__=='__main__':main()
