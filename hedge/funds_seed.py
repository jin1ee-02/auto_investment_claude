"""Starter list of well-known 13F filers. An editorial list, not a performance ranking.

Every CIK is verified against the filing source the first time it is refreshed;
the name stored afterwards is the one the source reports.
"""
from . import db

# (cik, display name, key person, style)
FEATURED = [
    ('1067983', 'Berkshire Hathaway', 'Warren Buffett', '집중 가치투자'),
    ('1536411', 'Duquesne Family Office', 'Stanley Druckenmiller', '매크로 · 성장'),
    ('2026053', 'Pershing Square', 'Bill Ackman', '액티비스트 · 집중'),
    ('2045724', 'Situational Awareness', 'Leopold Aschenbrenner', 'AI 집중투자'),
    ('1656456', 'Appaloosa Management', 'David Tepper', '기회주의 · 가치'),
    ('1647251', 'TCI Fund Management', 'Chris Hohn', '집중 장기투자'),
    ('1709323', 'Himalaya Capital', 'Li Lu', '집중 가치투자'),
    ('1549575', 'Dalal Street', 'Mohnish Pabrai', '집중 가치투자'),
    ('1061768', 'Baupost Group', 'Seth Klarman', '딥 밸류'),
    ('1489933', 'Greenlight Capital (DME)', 'David Einhorn', '가치 롱/숏'),
    ('1791786', 'Elliott Investment Management', 'Paul Singer', '액티비스트'),
    ('1112520', 'Akre Capital Management', 'Chuck Akre', '퀄리티 복리'),
    ('1569205', 'Fundsmith', 'Terry Smith', '퀄리티 장기투자'),
    ('1387322', 'Whale Rock Capital', 'Alex Sacerdote', '테크 · 성장'),
    ('1040273', 'Third Point', 'Dan Loeb', '이벤트 드리븐'),
    ('1135730', 'Coatue Management', 'Philippe Laffont', '테크 · 성장'),
    ('1167483', 'Tiger Global Management', 'Chase Coleman', '테크 · 성장'),
    ('1541617', 'Altimeter Capital', 'Brad Gerstner', '테크 · 성장'),
    ('1061165', 'Lone Pine Capital', 'Stephen Mandel', '성장주 롱/숏'),
    ('1103804', 'Viking Global Investors', 'Andreas Halvorsen', '펀더멘털 롱/숏'),
    ('1747057', 'D1 Capital Partners', 'Daniel Sundheim', '성장 롱/숏'),
    ('1029160', 'Soros Fund Management', 'George Soros', '글로벌 매크로'),
    ('1350694', 'Bridgewater Associates', 'Ray Dalio', '글로벌 매크로'),
    ('923093', 'Tudor Investment', 'Paul Tudor Jones', '매크로 · 멀티전략'),
    ('1697748', 'ARK Investment Management', 'Cathie Wood', '혁신 테마'),
    ('949509', 'Oaktree Capital Management', 'Howard Marks', '크레딧 · 가치'),
    ('1603466', 'Point72 Asset Management', 'Steve Cohen', '멀티전략'),
    ('1423053', 'Citadel Advisors', 'Ken Griffin', '멀티전략 · 마켓메이킹'),
    ('1037389', 'Renaissance Technologies', 'Jim Simons', '퀀트'),
    ('1045810', 'Nvidia', 'Jensen Huang', '기업 투자 포트폴리오'),
]

# A manager that started filing under a new CIK: quarters the old filer reported are added to
# the new one's, so the first quarter after the switch is not read as a wave of new buys.
PREDECESSORS = {
    '2026053': ('1336528',),     # Pershing Square Inc. <- Pershing Square Capital Management, L.P. (through 2026 Q1)
}

DEFAULT_SELECTION = ['1067983', '1536411', '2026053', '1656456', '1040273', '1135730', '1061165', '1103804']


def ensure_seed():
    if db.kv_get('seeded'):
        return
    with db.tx() as c:
        for cik, name, manager, style in FEATURED:
            c.execute('INSERT OR IGNORE INTO funds(cik,name,manager,style,featured) VALUES(?,?,?,?,1)',
                      (cik, name, manager, style))
    db.kv_set('seeded', True)
    if db.kv_get('selection') is None:
        db.kv_set('selection', DEFAULT_SELECTION)
