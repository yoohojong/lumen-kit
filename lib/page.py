import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from urllib.parse import urlparse
from bs4 import BeautifulSoup

class ExposureArea(str, Enum):
    AB = 'AB'
    SMART_BLOCK = '스마트블록'
    POPULAR = '인기글'
    DUPLICATE = '중복노출'
    DUPLICATE_AB = '중복노출(AB)'
    DUPLICATE_SMART_BLOCK = '중복노출(스마트블록)'
    DUPLICATE_POPULAR = '중복노출(인기글)'
    UNEXPOSED = '미노출'
    DROPPED = '누락'
    DELETED = '삭제'
    FAILED = '실패'

@dataclass
class RankResult:
    exposure_area: ExposureArea = ExposureArea.UNEXPOSED
    integrated_rank: Optional[int] = None
    cafe_slot_rank: Optional[int] = None
    blog_slot_rank: Optional[int] = None
    in_jisikin: bool = False
    block_order: list[str] = field(default_factory=list)
    smart_block_name: Optional[str] = None
    parser_confidence: float = 0.0
    matched_url: Optional[str] = None
    rank_from_dom_cards: bool = True

def parse_search_result(html: str, target_url: Optional[str], link_set: Optional[set[str]]=None, cafe_slug_whitelist: Optional[set[str]]=None) -> RankResult:
    if not html or len(html) < 500:
        return RankResult()
    result = RankResult()
    result.block_order = _detect_block_order(html)
    if _parse_ab_list(html, target_url, result, link_set, cafe_slug_whitelist):
        result.exposure_area = ExposureArea.AB
    elif _parse_smart_blocks(html, target_url, result, link_set, cafe_slug_whitelist):
        result.exposure_area = ExposureArea.SMART_BLOCK
    elif _parse_popular(html, target_url, result, link_set, cafe_slug_whitelist):
        result.exposure_area = ExposureArea.POPULAR
    if result.exposure_area == ExposureArea.UNEXPOSED:
        _parse_bootstrap_json_fallback(html, target_url, result, link_set, cafe_slug_whitelist)
    _parse_jisikin(html, target_url or '', result)
    return result
_NON_SLOT_BLOCK_PATTERNS = ('네이버 메이트', '브랜드 콘텐츠', '뉴스', '숏텐츠')

def _is_slot_block(box, h2_text: str) -> bool:
    if any((p in h2_text for p in _NON_SLOT_BLOCK_PATTERNS)):
        return False
    return any(('cafe.naver.com' in a['href'] or 'blog.naver.com' in a['href'] for a in box.find_all('a', href=True)))

def _detect_block_order(html: str) -> list[str]:
    if not html:
        return []
    soup = BeautifulSoup(html, 'lxml')
    boxes = soup.select('.desktop_mode.api_subject_bx, .fds-default-mode.api_subject_bx')
    seen: list[str] = []
    for box in boxes:
        h2 = box.find('h2')
        kind: Optional[str] = None
        if h2 is None:
            has_cafe = any(('cafe.naver.com' in a.get('href', '') for a in box.find_all('a', href=True)))
            if has_cafe and _extract_main_link(box):
                kind = ExposureArea.AB.value
        else:
            h2_text = h2.get_text(strip=True)
            if any((p in h2_text for p in _POPULAR_SKIP_PATTERNS)):
                continue
            if '인기글' in h2_text:
                kind = ExposureArea.POPULAR.value
            elif _is_slot_block(box, h2_text):
                kind = ExposureArea.SMART_BLOCK.value
            else:
                continue
        if kind and kind not in seen:
            seen.append(kind)
    if seen:
        return seen
    for box in boxes:
        if box.find('h2') is not None:
            continue
        if _extract_main_link(box):
            return [ExposureArea.AB.value]
    return []

def _extract_cafe_slug(url: str) -> Optional[str]:
    if not url or 'cafe.naver.com' not in url:
        return None
    from urllib.parse import urlparse
    p = urlparse(url)
    path_parts = [s for s in p.path.split('/') if s]
    if not path_parts:
        return None
    if path_parts[0] == 'ca-fe':
        return None
    return path_parts[0]

def _parse_ab_list(html: str, target_url: Optional[str], result: RankResult, link_set: Optional[set[str]]=None, cafe_slug_whitelist: Optional[set[str]]=None) -> bool:
    if not html:
        return False
    soup = BeautifulSoup(html, 'lxml')
    boxes = soup.select('.desktop_mode.api_subject_bx, .fds-default-mode.api_subject_bx')
    ab_items: list[tuple[str, str]] = []
    for box in boxes:
        if box.find('h2') is not None:
            continue
        has_cafe = any(('cafe.naver.com' in a.get('href', '') for a in box.find_all('a', href=True)))
        if not has_cafe:
            continue
        main_url = _extract_main_link(box)
        if not main_url:
            continue
        ab_items.append((main_url, _classify_item_url(main_url)))
    cafe_count = 0
    blog_count = 0
    for idx, (url, kind) in enumerate(ab_items, start=1):
        if kind == 'cafe':
            cafe_count += 1
        elif kind == 'blog':
            blog_count += 1
        if target_url is not None:
            if _urls_match(url, target_url):
                result.integrated_rank = idx
                if kind == 'cafe':
                    result.cafe_slot_rank = cafe_count
                elif kind == 'blog':
                    result.blog_slot_rank = blog_count
                result.parser_confidence = 0.9
                result.matched_url = target_url
                return True
            continue
        if link_set:
            if kind != 'cafe':
                continue
            if _urls_match_any(url, link_set):
                result.integrated_rank = idx
                result.cafe_slot_rank = cafe_count
                result.parser_confidence = 0.9
                result.matched_url = url
                print(f'    [AB_MATCH] idx={idx} kind={kind} matched_url={url[:90]}')
                return True
            continue
        if cafe_slug_whitelist and kind == 'cafe':
            slug = _extract_cafe_slug(url)
            if slug and slug in cafe_slug_whitelist:
                result.integrated_rank = idx
                result.cafe_slot_rank = cafe_count
                result.parser_confidence = 0.85
                result.matched_url = url
                print(f'    [AB_SLUG_MATCH] idx={idx} slug={slug} matched_url={url[:90]}')
                return True
    return False
_AD_LINK_PATTERNS = ('ader.naver.com', 'adcr.naver.com', '/ad/', '?adidx=')
_SIDEBAR_LINK_PATTERNS = ('hashtag', 'related_keyword', '/?query=')

def _is_excluded_link(url: str) -> bool:
    if not url:
        return True
    return any((p in url for p in _AD_LINK_PATTERNS)) or any((p in url for p in _SIDEBAR_LINK_PATTERNS))

def _extract_main_link(box) -> str:
    for sel in ['.total_tit a[href]', '.title_link a[href]', 'a.api_txt_lines[href]', '.title_area a[href]', '.user_thumb a[href]']:
        a = box.select_one(sel)
        if a:
            href = a.get('href', '')
            if href.startswith('http') and (not href.startswith(('javascript:', '#'))):
                if _is_excluded_link(href):
                    continue
                return href
    main_link = ''
    main_text_len = 0
    for a in box.find_all('a', href=True):
        href = a['href']
        if not href.startswith('http'):
            continue
        if href.startswith(('javascript:', '#')):
            continue
        if _is_excluded_link(href):
            continue
        text_len = len(a.get_text(strip=True))
        if text_len > main_text_len:
            main_text_len = text_len
            main_link = href
    return main_link

def _classify_item_url(url: str) -> str:
    if 'cafe.naver.com' in url:
        return 'cafe'
    if 'blog.naver.com' in url:
        return 'blog'
    return 'web'

def _normalize_netloc(netloc: str) -> str:
    if netloc.startswith('m.'):
        return netloc[2:]
    return netloc
_CAFE_NEW_URL_RE = re.compile('/ca-fe/cafes/(\\d+)/articles/(\\d+)')

def _normalize_cafe_url(parsed_url) -> tuple:
    netloc = _normalize_netloc(parsed_url.netloc)
    path = parsed_url.path.rstrip('/')
    m = _CAFE_NEW_URL_RE.match(path)
    if m:
        post_id = m.group(2)
        return (netloc, f'NEW:{post_id}')
    path_parts = [s for s in path.split('/') if s]
    if len(path_parts) >= 2 and path_parts[-1].isdigit():
        post_id = path_parts[-1]
        return (netloc, f'OLD:{post_id}')
    return (netloc, path)

def _urls_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    pa, pb = (urlparse(a), urlparse(b))
    na, ka = _normalize_cafe_url(pa)
    nb, kb = _normalize_cafe_url(pb)
    if na == nb and ka == kb:
        return True
    if na == nb:
        if ka.startswith('OLD:') and kb.startswith('NEW:'):
            return ka[4:] == kb[4:]
        if ka.startswith('NEW:') and kb.startswith('OLD:'):
            return ka[4:] == kb[4:]
    return False

def _urls_match_any(url: str, link_set: set[str]) -> bool:
    if not url or not link_set:
        return False
    return any((_urls_match(url, link) for link in link_set))
_SMART_BLOCK_SKIP_PATTERNS = ('인기글', '관련 브랜드 콘텐츠', '이미지', 'AI 브리핑', '네이버 클립', '네이버 가격비교', '네이버플러스 스토어')

def _parse_smart_blocks(html: str, target_url: Optional[str], result: RankResult, link_set: Optional[set[str]]=None, cafe_slug_whitelist: Optional[set[str]]=None) -> bool:
    if not html:
        return False
    soup = BeautifulSoup(html, 'lxml')
    boxes = soup.select('.desktop_mode.api_subject_bx, .fds-default-mode.api_subject_bx')
    for box in boxes:
        h2 = box.find('h2')
        if h2 is None:
            continue
        h2_text = h2.get_text(strip=True)
        if any((p in h2_text for p in _SMART_BLOCK_SKIP_PATTERNS)):
            continue
        if any((p in h2_text for p in _POPULAR_SKIP_PATTERNS)):
            continue
        if '인기글' in h2_text:
            continue
        cards, dom_ok = _items_by_card(box)
        cafe_count = 0
        for idx, card_urls in enumerate(cards, start=1):
            if any(('cafe.naver.com' in u for u in card_urls)):
                cafe_count += 1
            for url in card_urls:
                is_cafe = 'cafe.naver.com' in url
                if target_url is not None:
                    if _urls_match(url, target_url):
                        result.integrated_rank = idx
                        if is_cafe:
                            result.cafe_slot_rank = cafe_count
                        result.smart_block_name = h2_text
                        result.parser_confidence = 0.85
                        result.rank_from_dom_cards = dom_ok
                        result.matched_url = target_url
                        return True
                    continue
                if link_set:
                    if not is_cafe:
                        continue
                    if _urls_match_any(url, link_set):
                        result.integrated_rank = idx
                        result.cafe_slot_rank = cafe_count
                        result.smart_block_name = h2_text
                        result.parser_confidence = 0.85
                        result.rank_from_dom_cards = dom_ok
                        result.matched_url = url
                        print(f'    [SMART_BLOCK_MATCH] idx={idx} h2={h2_text!r} matched_url={url[:90]}')
                        return True
                    continue
                if cafe_slug_whitelist and is_cafe:
                    slug = _extract_cafe_slug(url)
                    if slug and slug in cafe_slug_whitelist:
                        result.integrated_rank = idx
                        result.cafe_slot_rank = cafe_count
                        result.smart_block_name = h2_text
                        result.parser_confidence = 0.85
                        result.rank_from_dom_cards = dom_ok
                        result.matched_url = url
                        print(f'    [SMART_BLOCK_SLUG_MATCH] idx={idx} slug={slug} h2={h2_text!r} matched_url={url[:90]}')
                        return True
    return False

def _extract_smart_block_items(box) -> list[str]:
    seen: set[tuple[str, str]] = set()
    items: list[str] = []
    for a in box.find_all('a', href=True):
        href = a['href']
        if not href.startswith('http'):
            continue
        if 'keep.naver.com' in href:
            continue
        p = urlparse(href)
        if p.path in ('', '/'):
            continue
        key = (p.netloc, p.path.rstrip('/'))
        if key in seen:
            continue
        seen.add(key)
        items.append(href)
    return items
_POPULAR_SKIP_PATTERNS = ('관련 브랜드 콘텐츠', '이미지', 'AI 브리핑', '네이버 클립', '네이버 가격비교', '네이버플러스 스토어', 'AI 추천', '숏폼', '플레이스', '동영상', '쇼핑')

def _parse_popular(html: str, target_url: Optional[str], result: RankResult, link_set: Optional[set[str]]=None, cafe_slug_whitelist: Optional[set[str]]=None) -> bool:
    if not html:
        return False
    soup = BeautifulSoup(html, 'lxml')
    boxes = soup.select('.desktop_mode.api_subject_bx, .fds-default-mode.api_subject_bx')
    for box in boxes:
        h2 = box.find('h2')
        if h2 is None:
            continue
        h2_text = h2.get_text(strip=True)
        if any((p in h2_text for p in _POPULAR_SKIP_PATTERNS)):
            continue
        if '인기글' not in h2_text:
            continue
        cards, dom_ok = _items_by_card(box)
        cafe_count = 0
        for idx, card_urls in enumerate(cards, start=1):
            if any(('cafe.naver.com' in u for u in card_urls)):
                cafe_count += 1
            for url in card_urls:
                is_cafe = 'cafe.naver.com' in url
                if target_url is not None:
                    if _urls_match(url, target_url):
                        result.integrated_rank = idx
                        if is_cafe:
                            result.cafe_slot_rank = cafe_count
                        result.smart_block_name = h2_text
                        result.parser_confidence = 0.85
                        result.rank_from_dom_cards = dom_ok
                        result.matched_url = target_url
                        return True
                    continue
                if link_set:
                    if not is_cafe:
                        continue
                    if _urls_match_any(url, link_set):
                        result.integrated_rank = idx
                        result.cafe_slot_rank = cafe_count
                        result.smart_block_name = h2_text
                        result.parser_confidence = 0.85
                        result.rank_from_dom_cards = dom_ok
                        result.matched_url = url
                        print(f'    [POPULAR_MATCH] idx={idx} h2={h2_text!r} matched_url={url[:90]}')
                        return True
                    continue
                if cafe_slug_whitelist and is_cafe:
                    slug = _extract_cafe_slug(url)
                    if slug and slug in cafe_slug_whitelist:
                        result.integrated_rank = idx
                        result.cafe_slot_rank = cafe_count
                        result.smart_block_name = h2_text
                        result.parser_confidence = 0.85
                        result.rank_from_dom_cards = dom_ok
                        result.matched_url = url
                        print(f'    [POPULAR_SLUG_MATCH] idx={idx} slug={slug} h2={h2_text!r} matched_url={url[:90]}')
                        return True
    return False

def _extract_popular_items(box) -> list[str]:
    seen_urls: set[tuple[str, str]] = set()
    items: list[str] = []
    for a in box.find_all('a', href=True):
        href = a['href']
        if not href.startswith('http'):
            continue
        if 'keep.naver.com' in href:
            continue
        p = urlparse(href)
        path_parts = [s for s in p.path.split('/') if s]
        if len(path_parts) < 2:
            continue
        last_seg = path_parts[-1].split('?')[0]
        if not last_seg.isdigit():
            continue
        url_key = (p.netloc, p.path.rstrip('/'))
        if url_key in seen_urls:
            continue
        seen_urls.add(url_key)
        items.append(href)
    return items

def _extract_popular_cards(box) -> list[list[str]]:
    containers = box.select("div[class*='fds-ugc']")
    if not containers:
        return []
    root = containers[0]
    cards: list[list[str]] = []
    seen_urls: set[tuple[str, str]] = set()
    for child in root.find_all(recursive=False):
        if 'sds-comps-divider' in ' '.join(child.get('class', [])):
            continue
        fresh: list[str] = []
        for url in _extract_popular_items(child):
            p = urlparse(url)
            key = (p.netloc, p.path.rstrip('/'))
            if key in seen_urls:
                continue
            seen_urls.add(key)
            fresh.append(url)
        if fresh:
            cards.append(fresh)
    if not cards:
        return []
    if seen_urls != {(urlparse(u).netloc, urlparse(u).path.rstrip('/')) for u in _extract_popular_items(box)}:
        return []

    def _confident_owners(card: list[str]) -> set:
        return {k for k in (slot_owner_key(u) for u in card) if not str(k[1]).startswith('?')}
    if any((len(_confident_owners(card)) > 1 for card in cards)):
        return []
    return cards

def slot_owner_key(url: str) -> tuple:
    if not url:
        return ('', '')
    parsed = urlparse(url)
    netloc = _normalize_netloc(parsed.netloc)
    parts = [seg for seg in parsed.path.split('/') if seg]
    if netloc == 'cafe.naver.com':
        if len(parts) == 2 and parts[1].isdigit():
            return (netloc, parts[0])
        if len(parts) >= 3 and parts[0] == 'ca-fe' and (parts[1] == 'cafes'):
            return (netloc, f'cafes/{parts[2]}')
        return (netloc, '?' + parsed.path.rstrip('/'))
    if netloc == 'blog.naver.com':
        if len(parts) == 2 and parts[1].isdigit():
            return (netloc, parts[0])
        if len(parts) == 3 and parts[1] == 'clip':
            return (netloc, parts[0])
    if netloc == 'in.naver.com' and len(parts) >= 2 and (parts[1] == 'contents'):
        return (netloc, parts[0])
    return (netloc, '?' + parsed.path.rstrip('/'))

def _owner_runs(urls: list[str]) -> list[list[str]]:
    runs: list[list[str]] = []
    for url in urls:
        if runs and slot_owner_key(url) == slot_owner_key(runs[-1][0]):
            runs[-1].append(url)
        else:
            runs.append([url])
    return runs
rank_signals = {'fallback_boxes': 0, 'mismatch_boxes': 0}

def reset_rank_signals() -> None:
    for key in rank_signals:
        rank_signals[key] = 0

def _items_by_card(box) -> tuple[list[list[str]], bool]:
    flat = _extract_popular_items(box)
    runs = _owner_runs(flat)
    cards = _extract_popular_cards(box)
    if not cards:
        if flat:
            rank_signals['fallback_boxes'] += 1
            print(f'    [CARD_FALLBACK] DOM 칸을 못 읽어 출처 묶기로 계산 ({len(runs)}칸)')
        return (runs, False)
    gap = len(cards) - len(runs)
    if gap:
        if gap >= 2:
            rank_signals['mismatch_boxes'] += 1
        print(f'    [CARD_MISMATCH] DOM 칸 {len(cards)} ≠ 출처 묶기 {len(runs)}' + ('' if gap >= 2 else ' (차이 1 — 같은 카페 이웃 칸으로 설명됨)'))
    return (cards, True)
_JISIKIN_H2_PATTERNS = ('지식iN', '지식인', '지식 iN')

def _parse_jisikin(html: str, target_url: str, result: RankResult) -> None:
    if not html:
        return
    soup = BeautifulSoup(html, 'lxml')
    for el in soup.select('[class*="sds-comps-profile-info-title"]'):
        if any((p in el.get_text(strip=True) for p in _JISIKIN_H2_PATTERNS)):
            result.in_jisikin = True
            return
    boxes = soup.select('.desktop_mode.api_subject_bx, .fds-default-mode.api_subject_bx')
    for box in boxes:
        h2 = box.find('h2')
        if not h2:
            continue
        h2_text = h2.get_text(strip=True)
        if not any((p in h2_text for p in _JISIKIN_H2_PATTERNS)):
            continue
        for a in box.find_all('a', href=True):
            if 'kin.naver.com' in a['href']:
                result.in_jisikin = True
                return
_BOOTSTRAP_PREFIX_RE = re.compile('entry\\.bootstrap\\(\\s*document\\.getElementById\\([^)]+\\)\\s*,\\s*')

def _extract_bootstrap_json(html: str) -> Optional[dict]:
    if not html or 'entry.bootstrap' not in html:
        return None
    match = _BOOTSTRAP_PREFIX_RE.search(html)
    if not match:
        return None
    start = match.end()
    if start >= len(html) or html[start] != '{':
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(html)):
        c = html[i]
        if escape:
            escape = False
            continue
        if c == '\\':
            escape = True
            continue
        if c == '"' and (not escape):
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:i + 1])
                except (json.JSONDecodeError, ValueError):
                    return None
    return None
_URL_LIKE_PREFIXES = ('http://', 'https://')

def _collect_urls_from_json(node, urls: list) -> None:
    if isinstance(node, dict):
        for v in node.values():
            _collect_urls_from_json(v, urls)
    elif isinstance(node, list):
        for v in node:
            _collect_urls_from_json(v, urls)
    elif isinstance(node, str):
        if node.startswith(_URL_LIKE_PREFIXES):
            urls.append(node)

def _parse_bootstrap_json_fallback(html: str, target_url: Optional[str], result: RankResult, link_set: Optional[set[str]]=None, cafe_slug_whitelist: Optional[set[str]]=None) -> bool:
    if not html:
        return False
    payload = _extract_bootstrap_json(html)
    if payload is None:
        return False
    raw_urls: list[str] = []
    _collect_urls_from_json(payload, raw_urls)
    if not raw_urls:
        return False
    seen: set[str] = set()
    urls: list[str] = []
    for u in raw_urls:
        if u in seen:
            continue
        if _is_excluded_link(u):
            continue
        seen.add(u)
        urls.append(u)
    if not urls:
        return False
    cafe_count = 0
    for idx, url in enumerate(urls, start=1):
        kind = _classify_item_url(url)
        if kind == 'cafe':
            cafe_count += 1
        if target_url is not None:
            if _urls_match(url, target_url):
                result.integrated_rank = idx
                if kind == 'cafe':
                    result.cafe_slot_rank = cafe_count
                result.exposure_area = ExposureArea.AB
                result.parser_confidence = 0.75
                result.rank_from_dom_cards = False
                result.matched_url = target_url
                print(f'    [JSON_FALLBACK_MATCH] idx={idx} kind={kind} matched_url={target_url[:90]}')
                return True
            continue
        if link_set:
            if kind != 'cafe':
                continue
            if _urls_match_any(url, link_set):
                result.integrated_rank = idx
                result.cafe_slot_rank = cafe_count
                result.exposure_area = ExposureArea.AB
                result.parser_confidence = 0.75
                result.rank_from_dom_cards = False
                result.matched_url = url
                print(f'    [JSON_FALLBACK_MATCH] idx={idx} kind={kind} matched_url={url[:90]}')
                return True
            continue
        if cafe_slug_whitelist and kind == 'cafe':
            slug = _extract_cafe_slug(url)
            if slug and slug in cafe_slug_whitelist:
                result.integrated_rank = idx
                result.cafe_slot_rank = cafe_count
                result.exposure_area = ExposureArea.AB
                result.parser_confidence = 0.7
                result.rank_from_dom_cards = False
                result.matched_url = url
                print(f'    [JSON_FALLBACK_SLUG_MATCH] idx={idx} slug={slug} matched_url={url[:90]}')
                return True
    return False

@dataclass
class SlotItem:
    area: str
    rank: int
    url: str
    kind: str
    title: str = ''
    block_name: str = ''
    source_name: str = ''

def _owner_key(url: str) -> tuple:
    if not url:
        return ()
    parsed = urlparse(url)
    netloc = _normalize_netloc(parsed.netloc)
    parts = [seg for seg in parsed.path.split('/') if seg]
    if not parts:
        return ()
    if len(parts) >= 3 and parts[0] == 'ca-fe' and (parts[1] == 'cafes'):
        return (netloc, f'cafes/{parts[2]}')
    return (netloc, parts[0])

def _collect_source_names(box) -> dict:
    names: dict = {}
    for a in box.find_all('a', href=True):
        href = a['href']
        if not href.startswith('http'):
            continue
        parsed = urlparse(href)
        parts = [seg for seg in parsed.path.split('/') if seg]
        is_home = len(parts) == 1 or (len(parts) == 3 and parts[0] == 'ca-fe' and (parts[1] == 'cafes'))
        if not is_home:
            continue
        text = _clean_source_name(a.get_text(strip=True))
        if not text:
            continue
        key = _owner_key(href)
        if key and key not in names:
            names[key] = text
    return names
_NAME_NOISE = ('새 창 열림', '새창열림', '새 창열림', '바로가기')
_NAME_TAIL_RE = re.compile('카페\\s*\\d[\\d.,]*\\s*[만천]?\\s*인용.*$|\\d[\\d.,]*\\s*[만천]\\s*인용.*$')

def _clean_source_name(text: str) -> str:
    if not text:
        return ''
    for noise in _NAME_NOISE:
        text = text.replace(noise, ' ')
    text = _NAME_TAIL_RE.sub('', text)
    text = re.sub('\\s+', ' ', text).strip(' ·-–—|')
    return text[:80]

def _title_for_url(box, url: str) -> str:
    if not url:
        return ''
    for a in box.find_all('a', href=True):
        if a['href'] == url:
            text = a.get_text(strip=True)
            for noise in _NAME_NOISE:
                text = text.replace(noise, ' ')
            text = re.sub('\\s+', ' ', text).strip()
            if text:
                return text[:120]
    return ''

def collect_slot_items(html: str, *, max_per_area: int=20) -> list[SlotItem]:
    if not html or len(html) < 500:
        return []
    soup = BeautifulSoup(html, 'lxml')
    boxes = soup.select('.desktop_mode.api_subject_bx, .fds-default-mode.api_subject_bx')
    items: list[SlotItem] = []
    ab_rank = 0
    for box in boxes:
        names = _collect_source_names(box)
        h2 = box.find('h2')
        if h2 is None:
            has_cafe = any(('cafe.naver.com' in a.get('href', '') for a in box.find_all('a', href=True)))
            if not has_cafe:
                continue
            url = _extract_main_link(box)
            if not url:
                continue
            ab_rank += 1
            if ab_rank > max_per_area:
                continue
            source_name = names.get(_owner_key(url), '')
            if not source_name and len(names) == 1:
                source_name = next(iter(names.values()))
            items.append(SlotItem(area=ExposureArea.AB.value, rank=ab_rank, url=url, kind=_classify_item_url(url), title=_title_for_url(box, url), source_name=source_name))
            continue
        h2_text = h2.get_text(strip=True)
        if any((p in h2_text for p in _POPULAR_SKIP_PATTERNS)):
            continue
        area = ExposureArea.POPULAR.value if '인기글' in h2_text else ExposureArea.SMART_BLOCK.value
        urls = _extract_popular_items(box)
        for idx, url in enumerate(urls[:max_per_area], start=1):
            items.append(SlotItem(area=area, rank=idx, url=url, kind=_classify_item_url(url), title=_title_for_url(box, url), block_name=h2_text, source_name=names.get(_owner_key(url), '')))
    return items

def is_known_url(url: str, link_set: Optional[set[str]]) -> bool:
    if not url or not link_set:
        return False
    return _urls_match_any(url, link_set)

def cafe_slug_of(url: str) -> Optional[str]:
    return _extract_cafe_slug(url)
