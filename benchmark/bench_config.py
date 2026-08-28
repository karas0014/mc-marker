# -*- coding: utf-8 -*-
"""Fixed inputs for the model benchmark.

The topic map is the one thing the results workbook cannot supply: the marker
only records answers. It is derived here from the actual F5 ICT paper
("F5 2nd Exam ans.pdf"), so the weak-topic targets the engines compute are the
real curriculum topics a teacher would have typed on the analyse form -- not
"未分類", which would make both the report advice and the MCQ targets
meaningless.
"""

import os

# Cover text. This repo is public, so the school is not named here -- set
# EXAMLENS_BENCH_SCHOOL locally if you want it on the generated PDFs. Nothing
# about the benchmark depends on it; it only prints on the cover.
SUBJECT = os.environ.get('EXAMLENS_BENCH_SUBJECT', '資訊及通訊科技')
EXAM_NAME = os.environ.get('EXAMLENS_BENCH_EXAM', '下學期考試（中五）')
SCHOOL = os.environ.get('EXAMLENS_BENCH_SCHOOL', '')
TERM = os.environ.get('EXAMLENS_BENCH_TERM', '下學期')
PASS_RATIO = 0.5

# topic name -> question numbers, from reading the 40 MC questions in the
# answer paper. Covers all 40, no gaps.
TOPIC_SPEC = [
    ('資訊科技對社會的影響', [1, 40]),
    ('數據表示與編碼',       [2, 4, 6, 7, 11]),
    ('資訊處理與應用軟件',   [3, 5, 9, 10]),
    ('數據組織與資料庫',     [8]),
    ('電腦系統與硬件',       [12, 13, 14, 15, 16]),
    ('互聯網與網絡',         [17, 19, 21, 22]),
    ('資訊安全',             [18, 20, 23, 24, 25]),
    ('算法與程式編寫',       [26, 27, 28, 29, 30, 31, 32, 33, 34, 36, 37, 38]),
    ('人工智能與新興科技',   [35, 39]),
]

TOPICS = {q: name for name, qs in TOPIC_SPEC for q in qs}
TOPIC_ORDER = [name for name, _ in TOPIC_SPEC]

# nvidia/nemotron-3.5-lightning:free was dropped after testing: OpenRouter's
# Anthropic-compatible endpoint returned HTTP 200 with an empty body for it and
# the native endpoint hung, on two separate accounts. Nothing about the model
# was measurable, so its results were removed rather than reported as zeros.
MODELS = [
    'z-ai/glm-5.2:free',
    'nvidia/nemotron-3-ultra-550b-a55b:free',
    'nvidia/nemotron-3-super-120b-a12b:free',
    'minimax/minimax-m3:free',
    'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free',
]

# OpenRouter's Anthropic-compatible endpoint. The SDK appends /v1/messages,
# so the base URL must NOT already end in /v1 (same rule as app._norm_base_url).
BASE_URL = 'https://openrouter.ai/api'

RESULTS_XLSX = 'MC Results test.xlsx'
ANSWER_PDF = 'F5 2nd Exam ans.pdf'
