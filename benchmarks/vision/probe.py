#!/usr/bin/env python3
"""Synthetic provider-vision check, separate from end-to-end CLI claims.

No dependencies or real user images. Two requests maximum. Images are built in
memory. The emutools conversion diagnostic makes no API request. The direct
provider baseline bypasses the bridge's image conversion, explicitly.
"""
from pathlib import Path
import base64
import hashlib
import json
import os
import struct
import sys
import urllib.error
import urllib.request
import zlib

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from emutools import Config,anthropic_to_canon,openai_to_canon,build_upstream_payload

MODEL='deepseek-v4-flash-vision-exp'
MAX_OUTPUT=800
# Peak tariffs are deliberately used even during cheaper off-peak hours.
INPUT_RATE=.44/1_000_000
OUTPUT_RATE=1.32/1_000_000
LIMIT=.02
QUESTION=('Read the image. Return only one JSON object with exactly these keys: '
          'code (the four-character code in the dark header), blue_circles '
          '(integer count of blue circles), and triangle_color (lowercase color). '
          'Use null if a requested detail cannot be seen. No explanations.')
FONT={
 'D':['11110','10001','10001','10001','10001','10001','11110'],
 '7':['11111','00001','00010','00100','01000','01000','01000'],
 'K':['10001','10010','10100','11000','10100','10010','10001'],
 '4':['00010','00110','01010','10010','11111','00010','00010'],
 'R':['11110','10001','10001','11110','10100','10010','10001'],
 '2':['01110','10001','00001','00010','00100','01000','11111'],
 'M':['10001','11011','10101','10101','10001','10001','10001'],
 '9':['01110','10001','10001','01111','00001','00001','01110'],
}


def make_image(code,circles,triangle):
    width,height=640,420
    pixels=bytearray([255])*(width*height*3)
    def rect(x0,y0,x1,y1,color):
        row=bytes(color)*(x1-x0)
        for y in range(y0,y1):pixels[(y*width+x0)*3:(y*width+x1)*3]=row
    rect(0,0,width,95,(25,25,25))
    for letter,char in enumerate(code):
        for y,line in enumerate(FONT[char]):
            for x,bit in enumerate(line):
                if bit=='1':rect(228+letter*48+x*8,19+y*8,236+letter*48+x*8,27+y*8,(255,255,255))
    for center in ([110,320,530] if circles==3 else [320]):
        for y in range(128,209):
            half=int(max(0,1600-(y-168)**2)**.5)
            rect(center-half,y,center+half+1,y+1,(21,91,219))
    rect(60,285,131,356,(239,140,35));rect(510,285,581,356,(239,140,35))
    color=(20,140,68) if triangle=='green' else (217,39,39)
    for y in range(255,356):
        half=55*(y-255)//100;rect(320-half,y,321+half,y+1,color)
    raw=b''.join(b'\0'+pixels[y*width*3:(y+1)*width*3] for y in range(height))
    def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw,9))+chunk(b'IEND',b'')
    assert len(pixels)==width*height*3 and len(raw)==height*(1+width*3)
    return png


def image_count(payload):
    return sum(isinstance(part,dict) and part.get('type')=='image_url'
               for message in payload.get('messages',[])
               for part in (message.get('content',[]) if isinstance(message.get('content'),list) else []))


def conversion_diagnostic(image_url):
    cfg=Config(model_big=MODEL,model_small=MODEL)
    common={'model':MODEL,'max_tokens':MAX_OUTPUT}
    oa=dict(common,messages=[{'role':'user','content':[{'type':'text','text':QUESTION},{'type':'image_url','image_url':{'url':image_url}}]}])
    an=dict(common,messages=[{'role':'user','content':[{'type':'text','text':QUESTION},{'type':'image','source':{'type':'base64','media_type':'image/png','data':image_url.split(',',1)[1]}}]}])
    result={}
    for label,body,convert in [('openai',oa,openai_to_canon),('anthropic',an,anthropic_to_canon)]:
        payload=build_upstream_payload(convert(body,cfg),cfg,[],False)
        result[label]={'images_forwarded':image_count(payload),'omission_placeholder':'[image omitted:' in json.dumps(payload),'target_model':payload['model']}
    return result


def decode_answer(text):
    stripped=text.strip()
    if stripped.startswith('```') and stripped.endswith('```'):
        stripped='\n'.join(stripped.splitlines()[1:-1])
    return json.loads(stripped)


def main():
    key=os.environ.get('EMU_UPSTREAM_API_KEY') or os.environ.get('DEEPSEEK_API_KEY')
    if not key:raise SystemExit('Set an upstream key in the environment; never commit it.')
    spent=0.;results=[];conversion=None
    for ident,code,circles,color in [('a','D7K4',3,'green'),('b','R2M9',1,'red')]:
        image=make_image(code,circles,color)
        image_url='data:image/png;base64,'+base64.b64encode(image).decode('ascii')
        if conversion is None:conversion=conversion_diagnostic(image_url)
        # Official documented cap: 384 image tokens. Add a large text/format margin.
        reservation=(len(QUESTION.encode())+8192+384)*INPUT_RATE+MAX_OUTPUT*OUTPUT_RATE
        if spent+reservation>LIMIT:raise SystemExit('Vision probe budget exhausted before request')
        payload={'model':MODEL,'thinking':{'type':'disabled'},'temperature':0,'max_tokens':MAX_OUTPUT,
                 'messages':[{'role':'user','content':[{'type':'text','text':QUESTION},{'type':'image_url','image_url':{'url':image_url}}]}]}
        assert image_count(payload)==1 and 'tools' not in payload and 'functions' not in payload
        record={'fixture':ident,'image_sha256':hashlib.sha256(image).hexdigest(),'image_bytes':len(image),'expected':{'code':code,'blue_circles':circles,'triangle_color':color},'direct_provider_baseline':True}
        request=urllib.request.Request('https://api.deepseek.com/chat/completions',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+key})
        try:
            with urllib.request.urlopen(request,timeout=45) as response:data=json.load(response)
            usage=data.get('usage',{})
            observed=isinstance(usage.get('prompt_tokens'),int) and isinstance(usage.get('completion_tokens'),int)
            cost=(usage['prompt_tokens']*INPUT_RATE+usage['completion_tokens']*OUTPUT_RATE) if observed else reservation
            spent+=cost
            content=data.get('choices',[{}])[0].get('message',{}).get('content','') or ''
            record.update(usage=usage,peak_tariff_bound_usd=cost,answer_text=content[:4000],finish_reason=data.get('choices',[{}])[0].get('finish_reason'))
            try:
                answer=decode_answer(content)
                record['passed']=(isinstance(answer,dict) and set(answer)=={'code','blue_circles','triangle_color'} and isinstance(answer.get('blue_circles'),int) and not isinstance(answer.get('blue_circles'),bool) and answer==record['expected'])
            except (ValueError,TypeError):record['passed']=False
        except (urllib.error.URLError,ValueError,TimeoutError) as exc:
            spent+=reservation
            record.update(passed=False,error=type(exc).__name__,http_status=getattr(exc,'code',None),peak_tariff_bound_usd=reservation)
            results.append(record);break
        results.append(record)
        print(json.dumps({'event':'fixture_complete','fixture':ident,'passed':record['passed']}),flush=True)
    report={'model':MODEL,'direct_provider_results':results,'direct_provider_passed':sum(r['passed'] for r in results),'direct_provider_total':2,'bridge_conversion':conversion,'end_to_end_bridge_vision_pass':False,'peak_tariff_bound_usd':spent,'limit_usd':LIMIT,'max_paid_requests':2,'scope':'Direct provider vision baseline plus offline bridge conversion inspection; not a Claude Code vision run.'}
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
    return 0 if len(results)==2 and all(r['passed'] for r in results) else 1


if __name__=='__main__':raise SystemExit(main())
