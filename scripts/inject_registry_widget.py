#!/usr/bin/env python3
"""Inject the shared machine-registry widget into the 9 non-English dashboards."""
import json
import re
import subprocess
from pathlib import Path

BASE = Path("/data/data/com.termux/files/home/spider/fcoin/src/transport")

files_and_labels = {
    "dashboard_jp.html": {
        "machines":  "マシン / machines",
        "broadcast": "全員へ配信 / broadcast",
        "target":    "単発送信 / send to one",
        "online":    "オンライン",
        "loading":   "マシン読込中…",
        "empty":     "オンラインのマシンなし",
        "error":     "マシン一覧を取得できません",
    },
    "dashboard_fr.html": {
        "machines":  "machines",
        "broadcast": "diffuser à tous",
        "target":    "envoyer à une",
        "online":    "en ligne",
        "loading":   "chargement des machines…",
        "empty":     "aucune machine en ligne",
        "error":     "impossible de charger les machines",
    },
    "dashboard_de.html": {
        "machines":  "Maschinen",
        "broadcast": "an alle senden",
        "target":    "an eine senden",
        "online":    "online",
        "loading":   "lade Maschinen…",
        "empty":     "keine Maschinen online",
        "error":     "Maschinen konnten nicht geladen werden",
    },
    "dashboard_ar.html": {
        "machines":  "الآلات / machines",
        "broadcast": "بث للكل",
        "target":    "إرسال لواحدة",
        "online":    "متصل",
        "loading":   "جاري تحميل الآلات…",
        "empty":     "لا توجد آلات متصلة",
        "error":     "تعذر تحميل الآلات",
    },
    "dashboard_zh.html": {
        "machines":  "机器 / machines",
        "broadcast": "广播给所有人",
        "target":    "发送给一台",
        "online":    "在线",
        "loading":   "正在加载机器…",
        "empty":     "没有在线机器",
        "error":     "无法加载机器",
    },
    "dashboard_ko.html": {
        "machines":  "기계 / machines",
        "broadcast": "모두에게 브로드캐스트",
        "target":    "하나에게 보내기",
        "online":    "온라인",
        "loading":   "기계 로딩 중…",
        "empty":     "온라인 기계 없음",
        "error":     "기계를 불러올 수 없음",
    },
    "dashboard_es.html": {
        "machines":  "máquinas",
        "broadcast": "transmitir a todos",
        "target":    "enviar a una",
        "online":    "en línea",
        "loading":   "cargando máquinas…",
        "empty":     "sin máquinas en línea",
        "error":     "no se pudieron cargar las máquinas",
    },
    "dashboard_pt.html": {
        "machines":  "máquinas",
        "broadcast": "transmitir para todas",
        "target":    "enviar para uma",
        "online":    "online",
        "loading":   "a carregar máquinas…",
        "empty":     "sem máquinas online",
        "error":     "não foi possível carregar as máquinas",
    },
    "dashboard_hi.html": {
        "machines":  "मशीनें / machines",
        "broadcast": "सभी को भेजें",
        "target":    "एक को भेजें",
        "online":    "ऑनलाइन",
        "loading":   "मशीनें लोड हो रही हैं…",
        "empty":     "कोई मशीन ऑनलाइन नहीं",
        "error":     "मशीनें लोड नहीं हो सकीं",
    },
    "dashboard_ru.html": {
        "machines":  "машины",
        "broadcast": "транслировать всем",
        "target":    "отправить одной",
        "online":    "в сети",
        "loading":   "загрузка машин…",
        "empty":     "нет машин в сети",
        "error":     "не удалось загрузить машины",
    },
}

results = []
for fname, lbl in files_and_labels.items():
    path = BASE / fname
    src = path.read_text(encoding="utf-8")
    agent_id = fname.replace("dashboard_", "dashboard-").replace(".html", "")
    lbl_json = json.dumps(lbl, ensure_ascii=False)

    if "FcoinRegistry.init" in src:
        results.append(f"{fname}: ALREADY INJECTED")
        continue

    # 1. CSS link in <head>
    if "/static/registry.css" not in src:
        src = src.replace(
            "</head>",
            '  <link rel="stylesheet" href="/static/registry.css">\n</head>',
            1,
        )

    # 2. <div id="fcoin-registry"></div> before first <textarea
    if "fcoin-registry" not in src:
        m = re.search(r"<textarea[\s>]", src)
        if not m:
            results.append(f"{fname}: SKIP — no textarea")
            continue
        src = (
            src[:m.start()]
            + '  <div id="fcoin-registry"></div>\n\n  '
            + m.group(0)
            + src[m.end():]
        )

    # 3. Inject shared script + init block right before the first
    #    <script>(function() — the IIFE that holds the page's logic.
    m = re.search(r"<script>\s*\n\s*\(function\(\)", src)
    if not m:
        results.append(f"{fname}: SKIP — no IIFE")
        continue
    init = (
        '<script src="/static/registry.js"></script>\n'
        '<script>\n'
        '  FcoinRegistry.init({\n'
        "    anchor:  '#fcoin-registry',\n"
        f"    agentId: '{agent_id}',\n"
        f"    labels:  {lbl_json},\n"
        '  });\n'
        '</script>\n\n'
    )
    src = src[:m.start()] + init + src[m.start():]

    path.write_text(src, encoding="utf-8")
    results.append(f"{fname}: OK ({len(src)} bytes)")

print("\n".join(results))
