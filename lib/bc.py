import json
import re
from urllib.parse import unquote
from bs4 import BeautifulSoup
TOP_SLOTS = 3
BOXES = '.desktop_mode.api_subject_bx, .fds-default-mode.api_subject_bx'
BLOCK_NAME = '브랜드 콘텐츠'
AD_HOSTS = ('ader.naver.com', 'adcr.naver.com')
AD_MARK = re.compile('[?&]c=naver\\.search\\.pc\\.([A-Za-z0-9_]+)')
BLOCK_MARK = 'pwc_itb'
NOT_THIS = ('plsearch', 'npla')
NAME_NOISE = ('새 창 열림', '새창열림', '새 창열림', '바로가기')
DATE_RE = re.compile('\\d{4}\\.\\s?\\d{1,2}\\.\\s?\\d{1,2}\\.?|\\d+\\s?(?:분|시간|일|주|개월)\\s?전')
MIN_PAGE = 500

class UnreadablePage(ValueError):
    pass

def _clean(text):
    text = text or ''
    for n in NAME_NOISE:
        text = text.replace(n, ' ')
    return re.sub('\\s+', ' ', text).strip(' ·-|')

def _box_title(box):
    h2 = box.find('h2')
    return h2.get_text(strip=True) if h2 is not None else ''

def _ad_links(node):
    out = []
    for a in node.find_all('a', href=True):
        href = a['href']
        if not any((h in href for h in AD_HOSTS)):
            continue
        if a.find_parent(class_='nad_area') is not None:
            continue
        m = AD_MARK.search(href)
        if m and m.group(1) in NOT_THIS:
            continue
        out.append(a)
    return out

def _first_text(card, selectors):
    for sel in selectors:
        el = card.select_one(sel)
        if el is not None:
            t = _clean(el.get_text(' ', strip=True))
            if t:
                return t
    return ''

def _card(card, links):
    href = links[0]['href'] if links else card.get('data-power-content-url') or ''
    m = AD_MARK.search(href)
    name = _first_text(card, ('.sds-comps-profile-info-title-text', 'a[data-heatmap-target="articleSourceJSX_title"]'))
    if not name:
        name = next((_clean(a.get_text(' ', strip=True)) for a in links if _clean(a.get_text(' ', strip=True))), '')
    when = ''
    for el in card.select('.sds-comps-profile-info-subtext'):
        d = DATE_RE.search(el.get_text(' ', strip=True))
        if d:
            when = d.group(0)
            break
    return {'이름': name, '제목': _first_text(card, ('a[data-heatmap-target=".tit"]',)), '설명': _first_text(card, ('a[data-heatmap-target=".des"]',)), '날짜': when, '표식': m.group(1) if m else '', '열쇠': href.split('?', 1)[0]}

def _cards_in(box):
    found = []
    frames = box.select('._fe_view_power_content')
    for fr in frames:
        if fr.find_parent(class_='_fe_view_power_content') is not None:
            continue
        links = _ad_links(fr)
        any_ad = any((any((h in a['href'] for h in AD_HOSTS)) for a in fr.find_all('a', href=True)))
        if links or (not any_ad and fr.get('data-power-content-url') and (fr.find_parent(class_='nad_area') is None)):
            found.append(_card(fr, links))
    if found:
        return found
    seen = {}
    for a in _ad_links(box):
        key = a['href'].split('?', 1)[0]
        if key in seen:
            continue
        holder = a
        for parent in a.parents:
            if parent is box:
                break
            if len({x['href'].split('?', 1)[0] for x in _ad_links(parent)}) > 1:
                break
            holder = parent
        seen[key] = _card(holder, [x for x in _ad_links(holder)] or [a])
    return list(seen.values())

def _more_link(box):
    for a in box.find_all('a', href=True):
        cls = ' '.join(a.get('class') or [])
        if 'footer-more' in cls or '더보기' in a.get_text(strip=True):
            href = a['href']
            if href.startswith('#lb_api='):
                href = unquote(href[len('#lb_api='):])
            return href
    return ''

def _is_block(box):
    return BLOCK_NAME in _box_title(box)

def _rank(ads, start=1, shown=True):
    out, seen = ([], set())
    for ad in ads:
        if ad['열쇠'] and ad['열쇠'] in seen:
            continue
        seen.add(ad['열쇠'])
        out.append(ad)
    for i, ad in enumerate(out, start=start):
        ad['순위'] = i
        ad['더보기전'] = bool(shown and i <= TOP_SLOTS)
    return out

def read_brand_ads(html):
    if not html or len(html) < MIN_PAGE:
        raise UnreadablePage('empty or short page')
    soup = BeautifulSoup(html, 'lxml')
    boxes = soup.select(BOXES) or soup.select('.api_subject_bx')
    if not boxes:
        raise UnreadablePage('no result boxes')
    mine = [b for b in boxes if _is_block(b)]
    if not mine:
        mine = [b for b in boxes if any((BLOCK_MARK in a['href'] for a in _ad_links(b)))]
    ads = []
    for b in mine:
        ads.extend(_cards_in(b))
    return {'칸있음': bool(mine), '칸이름': _box_title(mine[0]) if mine else '', '광고': _rank(ads), '더보기': next((u for u in (_more_link(b) for b in mine) if u), ''), '더보기열어봄': False}

def _html_parts(raw):
    raw = raw or ''
    s = raw.lstrip()
    if s[:1] in '{[':
        try:
            node = json.loads(s)
        except ValueError:
            return [raw]
        parts, stack = ([], [node])
        while stack:
            v = stack.pop()
            if isinstance(v, dict):
                stack.extend(reversed(list(v.values())))
            elif isinstance(v, list):
                stack.extend(reversed(v))
            elif isinstance(v, str) and '<' in v:
                parts.append(v)
        return parts
    return [raw]

def add_more(main, more_pages):
    out = dict(main)
    have = list(main.get('광고') or [])
    keys = {a['열쇠'] for a in have}
    extra = []
    for raw in more_pages or ():
        for part in _html_parts(raw):
            soup = BeautifulSoup(part, 'lxml')
            boxes = [b for b in soup.select('.api_subject_bx') if _is_block(b)] or [soup]
            for b in boxes:
                for ad in _cards_in(b):
                    if ad['열쇠'] not in keys:
                        keys.add(ad['열쇠'])
                        extra.append(ad)
    out['광고'] = have + _rank(extra, start=len(have) + 1, shown=False)
    out['칸있음'] = bool(main.get('칸있음') or extra)
    out['더보기열어봄'] = True
    return out
