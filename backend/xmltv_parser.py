import xml.etree.ElementTree as ET
import os
import re

def parse_xmltv(file_path):
    """Parses an XMLTV file and extracts the channel definitions."""
    if not os.path.exists(file_path):
        return []
    
    channels = []
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        
        for channel in root.findall('channel'):
            channel_id = channel.get('id')
            display_name = channel.find('display-name')
            name = display_name.text if display_name is not None else channel_id
            
            icon = channel.find('icon')
            icon_url = icon.get('src') if icon is not None else ""
            
            channels.append({
                "id": channel_id,
                "name": name,
                "icon": icon_url,
                "source_file": os.path.basename(file_path)
            })
    except Exception as e:
        print(f"Error parsing {file_path}: {e}")
        
    return channels

def parse_episode(prog):
    """Extracts a human-readable season/episode string ("S2 E6") from the
    programme's <episode-num> tags. Prefers the machine-readable xmltv_ns
    system (zero-based "season.episode.part"), falling back to whatever the
    onscreen system provides (usually already "S02E06" style)."""
    onscreen = None
    for ep in prog.findall('episode-num'):
        system = ep.get('system', '')
        text = (ep.text or "").strip()
        if not text:
            continue
        if system == 'xmltv_ns':
            parts = text.split('.')
            def _num(idx):
                if idx >= len(parts):
                    return None
                token = parts[idx].split('/')[0].strip()
                try:
                    return int(token) + 1  # xmltv_ns is zero-based
                except ValueError:
                    return None
            season, episode = _num(0), _num(1)
            if season and episode:
                return f"S{season} E{episode}"
            if episode:
                return f"E{episode}"
        elif system == 'onscreen':
            onscreen = text
    return onscreen

# Categories where an untagged airing is NOT assumed to be a rerun
NON_EPISODIC_CATEGORIES = ('movie', 'film', 'paid program', 'shopping', 'sports')

# Newscast title patterns for feeds that carry no <category> data at all
# (zap2xml/Gracenote OTA feeds emit none) — extend to taste
NEWSCAST_TITLE_PREFIXES = ('good day ', 'good morning ')
NEWSCAST_EXACT_TITLES = ('today', 'early today', 'cbs mornings')

# Titles that never deserve a badge, whatever episode metadata they carry
UNBADGED_TITLES = ('paid programming', 'paid program')

def _is_newscast(title, categories):
    if any('news' in c for c in categories):
        return True
    t = (title or '').strip().lower()
    # \bnews\b: "News 8 at 11" and "PBS News Hour" match, the sitcom
    # "NewsRadio" does not
    return (
        re.search(r'\bnews\b', t) is not None
        or t in NEWSCAST_EXACT_TITLES
        or any(t.startswith(p) for p in NEWSCAST_TITLE_PREFIXES)
    )

def _has_episode_identity(prog, title):
    """A programme is a distinct episode if it has an episode-num, or a
    sub-title that isn't just the title repeated (shopping-channel feeds
    duplicate the title into sub-title on every segment)."""
    if prog.find('episode-num') is not None:
        return True
    sub = prog.find('sub-title')
    return sub is not None and bool(sub.text) and sub.text.strip() != (title or '').strip()

def parse_flag(prog):
    """Airing-status badge for a programme, tuned to real-world feeds:

    Gracenote-style feeds (zap2xml etc.) only ever tag <new/> — no <live/>,
    no <previously-shown/>, no categories. So a newscast tagged new is
    really airing live, and an untagged series episode is a repeat by
    convention. Some grabbers also stamp <premiere> on every shopping-
    channel segment; a premiere with no episode identity, no <new/>, and no
    text is ignored as junk. Movies/paid programming/sports get no badge
    when untagged."""
    title_node = prog.find('title')
    title = title_node.text if title_node is not None else ''
    if (title or '').strip().lower() in UNBADGED_TITLES:
        return None
    categories = [c.text.lower() for c in prog.findall('category') if c.text]
    is_news = _is_newscast(title, categories)
    episodic = _has_episode_identity(prog, title)
    is_new = prog.find('new') is not None

    premiere = prog.find('premiere')
    if premiere is not None and (episodic or is_new or (premiere.text or '').strip()):
        return "Premiere"
    if prog.find('live') is not None:
        return "Live"
    if is_new:
        return "Live" if is_news else "New"
    if prog.find('previously-shown') is not None:
        return "Rerun"
    if is_news:
        return "News"
    if any(word in c for c in categories for word in NON_EPISODIC_CATEGORIES):
        return None
    if episodic:
        return "Rerun"
    return None

def parse_xmltv_programs(file_path):
    """Parses an XMLTV file and extracts the programme schedules."""
    if not os.path.exists(file_path):
        return []

    programs = []
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()

        for prog in root.findall('programme'):
            channel_id = prog.get('channel')
            start = prog.get('start')
            stop = prog.get('stop')

            title_node = prog.find('title')
            title = title_node.text if title_node is not None else "Unknown Program"

            desc_node = prog.find('desc')
            desc = desc_node.text if desc_node is not None else ""

            # Episode name, e.g. <sub-title>The One With the Embassy Club</sub-title>
            sub_node = prog.find('sub-title')
            episode_title = sub_node.text.strip() if sub_node is not None and sub_node.text else None

            icon_node = prog.find('icon')
            image_url = icon_node.get('src') if icon_node is not None else None
            categories = [category.text.strip() for category in prog.findall('category') if category.text]

            programs.append({
                "channel_id": channel_id,
                "start": start,
                "stop": stop,
                "title": title,
                "description": desc,
                "episode": parse_episode(prog),
                "episode_title": episode_title,
                "flag": parse_flag(prog),
                "image_url": image_url,
                "categories": categories,
            })
    except Exception as e:
        print(f"Error parsing programmes from {file_path}: {e}")

    return programs
