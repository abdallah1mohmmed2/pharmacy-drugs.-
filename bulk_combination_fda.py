#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""US multi-ingredient label candidates; research only, never clinical equivalence."""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime,timezone
import json
from pathlib import Path
import re
from urllib.parse import urlencode
import os
import time
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
from bulk_fda_references import snippets,API

SYNONYMS={
    'paracetamol':'acetaminophen','salbutamol':'albuterol',
    'clavulanic acid':'clavulanate','sulphamethoxazole':'sulfamethoxazole',
    'hydrochlorothiazide':'hydrochlorothiazide',
}
BANNED=('multivitamin','mineral','formula','herbal','extract','shampoo','cosmetic')


def composition(source):
    name=str(source or '').strip().lower()
    parts=[s.strip() for s in name.split('+')]
    if not 2<=len(parts)<=4 or any(x in name for x in BANNED):
        return []
    if not all(re.fullmatch(r'[a-z][a-z -]{2,49}',x) for x in parts):
        return []
    return parts


def match(candidate,components):
    info=candidate.get('openfda') or {}
    names=info.get('generic_name',[]) or []
    substances=info.get('substance_name',[]) or []
    if isinstance(names,str):names=[names]
    if isinstance(substances,str):substances=[substances]
    allnames=' | '.join(str(x) for x in names+substances).lower()
    if not allnames:return False
    for component in components:
        terms=[component,SYNONYMS.get(component,component)]
        if not any(re.search(r'(?<![a-z])'+re.escape(term)+r'(?![a-z])',allnames) for term in terms):
            return False
    return True


def fetch(components,apikey):
    terms=[SYNONYMS.get(c,c) for c in components]
    search=' AND '.join('openfda.generic_name:"'+t+'"' for t in terms)
    params={'search':search,'limit':'10'}
    if apikey:params['api_key']=apikey
    url=API+'?'+urlencode(params)
    for attempt in range(3):
        try:
            time.sleep(0.65)
            with urlopen(Request(url,headers={'User-Agent':'Pharmacist-review-research/1.0','Accept':'application/json'}),timeout=23) as res:
                return json.load(res),'OK'
        except HTTPError as e:
            if e.code==404:return {},'NO_MATCH'
            if e.code in (400,401,403):return {},'HTTP_'+str(e.code)
            if e.code==429 and attempt<2:time.sleep(6*(attempt+1));continue
            if attempt==2:return {},'HTTP_'+str(e.code)
        except (URLError,TimeoutError,ValueError):
            if attempt==2:return {},'NETWORK_ERROR'
            time.sleep(2*(attempt+1))
    return {},'ERROR'


def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('--catalog',required=True)
    p.add_argument('--output',required=True)
    p.add_argument('--max-combinations',type=int,default=300)
    a=p.parse_args(argv)
    if not 1<=a.max_combinations<=500:p.error('max-combinations must be 1..500')
    catalog=json.loads(Path(a.catalog).read_text(encoding='utf-8'))
    if not isinstance(catalog,list) or len(catalog)<20000:p.error('Require complete converted catalog')
    combos=Counter(str(x.get('scientific_name') or '').strip().lower() for x in catalog)
    candidates=[(ing,freq,composition(ing)) for ing,freq in combos.items() if composition(ing)]
    candidates.sort(key=lambda t:(-t[1],t[0]))
    candidates=candidates[:a.max_combinations]
    statuses=Counter()
    seen={}
    for n,(ingredient,count,parts) in enumerate(candidates,1):
        data,status=fetch(parts,os.getenv('OPENFDA_API_KEY',''))
        statuses[status]+=1
        found=[]
        visited=set()
        for item in data.get('results',[]) if isinstance(data,dict) else []:
            if not isinstance(item,dict) or not match(item,parts):continue
            sid=str(item.get('set_id') or '')
            if sid in visited or not re.fullmatch(r'[0-9a-fA-F-]{36}',sid):continue
            section_text,truncated=snippets(item)
            if not section_text:continue
            visited.add(sid)
            info=item.get('openfda') or {}
            found.append({'set_id':sid,
               'url':'https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid='+sid,
               'date':item.get('effective_time',''),
               'US_brand_name':info.get('brand_name',[]),
               'US_generic_name':info.get('generic_name',[]),
               'US_substance_name':info.get('substance_name',[]),
               'US_routes':info.get('route',[]),
               'sections':section_text,
               'truncation':truncated,
               'all_requested_components_found_as_name_text':True,
               'absence_of_extra_components_verified':False,
               'exact_egyptian_product_equivalence_verified':False,
               'approved':False})
            if len(found)>=2:break
        if found:
            seen[ingredient]={'source_scientific_name':ingredient,
                              'catalog_records_with_this_name':count,
                              'required_components':parts,
                              'matches':found,
                              'verification_status':'POTENTIAL_US_MULTI_INGREDIENT_LABEL_ONLY'}
        if n%40==0:print('PROGRESS',n,'/',len(candidates),'found',len(seen),flush=True)
    output={'status':'COMBINATION_LABEL_CANDIDATES_REVIEW_ONLY',
      'created_utc':datetime.now(timezone.utc).isoformat(),
      'query_count':len(candidates),
      'matched_combinations':len(seen),
      'catalog_records_with_candidate_composition':sum(x['catalog_records_with_this_name'] for x in seen.values()),
      'outcomes':dict(statuses),
      'clinical_use_approved':False,
      'no_local_egyptian_brand_approved':True,
      'composition_candidates':seen}
    path=Path(a.output);path.parent.mkdir(exist_ok=True,parents=True)
    if path.exists():raise ValueError('Refuse to overwrite')
    path.write_text(json.dumps(output,ensure_ascii=False,separators=(',',':'))+'\n',encoding='utf-8')
    print('FINAL COMBINATION:',json.dumps({k:v for k,v in output.items() if k!='composition_candidates'}),flush=True)


if __name__=='__main__':main()
