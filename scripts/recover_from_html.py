from pathlib import Path
from collections import Counter
from urllib.parse import urlsplit, urlunsplit, unquote
import re, html, json, hashlib, difflib, zipfile, sys
from bs4 import BeautifulSoup, NavigableString, Comment
import markdown

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / 'site'
DOCS = ROOT / 'archived/recovery-2026-09-16/restored'
OUT = ROOT / 'archived/recovery-2026-09-16'
PAGES = sorted(p for group in ('devs','people','series') for p in (SITE/'games'/group).glob('*.html'))
EXT = ['tables','fenced_code','attr_list','pymdownx.superfences','pymdownx.mark','pymdownx.tilde']

def clean(soup):
    for x in soup.select('a.headerlink, a.md-content__button, script, style'):
        x.decompose()
    return soup

def normal(text):
    return re.sub(r'\s+', '', text)

def esc(text):
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    text = re.sub(r'([\\`*_\[\]|~=#!])', r'\\\1', text)
    text = re.sub(r'(?m)^(\s*)([-+])(?=\s)', r'\1\\\2', text)
    return re.sub(r'(?m)^(\s*\d+)([.)])(?=\s)', r'\1\\\2', text)

def converted_link(url, page):
    parts = urlsplit(url)
    if parts.scheme or parts.netloc or not parts.path:
        return url
    target = (page.parent / unquote(parts.path)).resolve()
    if target in PAGES:
        parts = parts._replace(path=re.sub(r'\.html$', '.md', parts.path))
    return urlunsplit(parts).replace(' ', '%20')

class Converter:
    def __init__(self,page):
        self.page=page
    def children(self,node):
        return ''.join(self.convert(x) for x in node.children)
    def convert(self,node):
        if isinstance(node,Comment): return ''
        if isinstance(node,NavigableString): return esc(str(node))
        tag=node.name
        if tag=='pre':
            code=node.find('code'); value=(code or node).get_text()
            runs=[len(x) for x in re.findall(r'`+',value)]
            fence='`'*max(3,max(runs,default=0)+1)
            classes=(code.get('class',[]) if code else [])+node.get('class',[])
            lang=next((x[len('language-'):] for x in classes if x.startswith('language-')),'')
            return '\n\n'+fence+lang+'\n'+value.rstrip('\n')+'\n'+fence+'\n\n'
        if tag=='code':
            value=node.get_text(); fence='`'*(max([len(x) for x in re.findall(r'`+',value)],default=0)+1)
            pad=' ' if value.startswith(('`',' ')) or value.endswith(('`',' ')) else ''
            return fence+pad+value+pad+fence
        if tag in ('ul','ol'):
            lines=[]; start=int(node.get('start',1))
            for i,li in enumerate(node.find_all('li',recursive=False)):
                body=self.children(li).strip(); marker=f'{start+i}. ' if tag=='ol' else '- '
                bits=body.splitlines(); lines.append(marker+bits[0]+'\n'+''.join('    '+x+'\n' for x in bits[1:]))
            return '\n\n'+'\n'.join(lines)+'\n\n'
        if tag=='table':
            rows=[]
            for tr in node.find_all('tr'):
                cells=[self.children(x).strip().replace('\n','<br>') for x in tr.find_all(['th','td'],recursive=False)]
                rows.append(cells)
            width=max(map(len,rows),default=0)
            if not width:return ''
            rows=[x+['']*(width-len(x)) for x in rows]
            result=['| '+' | '.join(rows[0])+' |','| '+' | '.join(['---']*width)+' |']
            result.extend('| '+' | '.join(row)+' |' for row in rows[1:])
            return '\n\n'+'\n'.join(result)+'\n\n'
        content=self.children(node)
        if re.fullmatch('h[1-6]',tag or ''):
            if not content.strip():return '\n\n'+str(node)+'\n\n'
            level=int(tag[1]); ident=node.get('id'); attrs=' { #'+ident+' }' if ident else ''
            return '\n\n'+'#'*level+' '+content.strip()+attrs+'\n\n'
        if tag=='p':return '\n\n'+content.strip()+'\n\n'
        if tag=='br':return '  \n'
        if tag=='hr':return '\n\n---\n\n'
        if tag in ('strong','b'):return str(node) if '\n' in content or content!=content.strip() else '**'+content+'**'
        if tag in ('em','i'):return str(node) if '\n' in content or content!=content.strip() else '*'+content+'*'
        if tag=='blockquote':return '\n\n'+'\n'.join('> '+x if x else '>' for x in content.strip().splitlines())+'\n\n'
        if tag=='a':
            anchor=''
            if node.get('id'):anchor='<a id="'+html.escape(node['id'],quote=True)+'"></a>'
            href=node.get('href')
            if href is None:return anchor+content
            href=converted_link(href,self.page)
            title=' "'+node['title'].replace('"','&quot;')+'"' if node.get('title') else ''
            return anchor+'['+content+'](<'+href+'>'+title+')'
        if tag in ('article','div','span'):return content
        return str(node)

def run():
    reports=[]
    backup=zipfile.ZipFile(OUT/'before-recovery.zip')
    for page in PAGES:
        if len(sys.argv)>1 and page.stem not in sys.argv[1:]:continue
        source=backup.read(str(page.relative_to(ROOT)));  soup=BeautifulSoup(source,'html.parser'); article=clean(soup.select_one('article.md-content__inner'))
        if not article:raise ValueError(str(page))
        title=page.stem; headings=article.find_all(re.compile('^h[1-6]$')); first=next((h for h in headings if h.name=='h1'),None)
        same=first and first.get_text(strip=True)==title
        for h in headings:
            if h is first and same:continue
            h.name='h'+str(min(6,int(h.name[1])+1))
        if not same:
            h=soup.new_tag('h1');h.string=title;article.insert(0,h)
        expected=normal(article.get_text())
        converter=Converter(page); md=converter.convert(article).strip()+'\n'
        rendered=BeautifulSoup(markdown.markdown(md,extensions=EXT),'html.parser')
        actual=normal(rendered.get_text())
        target=DOCS/page.relative_to(SITE).with_suffix('.md')

        if expected!=actual:
            m=difflib.SequenceMatcher(None,expected,actual,autojunk=False) if len(expected)<10000 else None
            offset=next((i for i,(a,b) in enumerate(zip(expected,actual)) if a!=b),min(len(expected),len(actual)))
            print('MISMATCH',page.name, 'offset',offset, 'expected',repr(expected[max(0,offset-70):offset+160]),'actual',repr(actual[max(0,offset-70):offset+160]),'lengths',len(expected),len(actual),flush=True)
            (OUT/'failed.md').write_text(md)
            raise ValueError('Text mismatch; no file created for '+page.name)
        old_code=[x.get_text() for x in article.select('pre')];new_code=[x.get_text() for x in rendered.select('pre')]
        assert [x.rstrip('\n') for x in old_code]==[x.rstrip('\n') for x in new_code],page.name+' code mismatch'
        source_ids=[x['id'] for x in article.select('[id]')];dest_ids=[x['id'] for x in rendered.select('[id]')]
        assert Counter(source_ids)==Counter(dest_ids),page.name+' anchors mismatch'
        source_links=[converted_link(x['href'],page) for x in article.select('a[href]')];dest_links=[x['href'] for x in rendered.select('a[href]')]
        assert source_links==dest_links,page.name+' links mismatch'
        assert len(article.select('table'))==len(rendered.select('table')),page.name+' tables mismatch'
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(md)
        reports.append(dict(source=str(page.relative_to(ROOT)),target=str(target.relative_to(ROOT)),source_sha256=hashlib.sha256(source).hexdigest(),markdown_sha256=hashlib.sha256(md.encode()).hexdigest(),characters=len(expected),headings=len(rendered.find_all(re.compile('^h[1-6]$'))),code_blocks=len(old_code),tables=len(article.select('table')),links=len(source_links),anchors=len(source_ids),visible_text_matches=True))
        print('OK',page.name,len(md),'chars',flush=True)
    report_path=OUT/'verification.json'
    previous=json.loads(report_path.read_text()) if report_path.exists() else []
    selected={x['source'] for x in reports}
    reports=[x for x in previous if x['source'] not in selected]+reports
    report_path.write_text(json.dumps(reports,ensure_ascii=False,indent=2)+'\n')

if __name__=='__main__':run()
