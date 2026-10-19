# DragShift macOS end-to-end — 15/16 passed

| # | Scenario | Result | Detail | Time |
|---|---|---|---|---|
| 1 | JPG → PNG | ✅ | D-Photo.png · 2400×1600 | 9.9s |
| 2 | JPG → WEBP | ✅ | D-Photo.webp · 2400×1600 | 10.0s |
| 3 | JPG → HEIC | ✅ | D-Photo.heic · 2400×1600 | 11.0s |
| 4 | JPG → PDF | ✅ | D-Photo.pdf · 1 page(s) | 9.2s |
| 5 | PNG → JPG | ✅ | D-Graphic.jpg · 800×533 | 8.7s |
| 6 | PDF → JPG (3 pages) | ✅ | 3 × .jpg | 9.3s |
| 7 | PDF → TXT | ✅ | D-Report.txt | 8.9s |
| 8 | DOCX → PDF | ✅ | D-Letter.pdf · 1 page(s) | 9.3s |
| 9 | PDF Split | ✅ | 3 PDFs | 10.1s |
| 10 | PDF Rotate | ✅ | all pages at 90° | 10.3s |
| 11 | PDF Compress | ❌ | AssertionError: no output file appeared | 24.2s |
| 12 | Merge 2 PDFs | ✅ | 5 pages | 13.1s |
| 13 | Compress (window, Enter) | ✅ | 354 KB → 112 KB | 11.6s |
| 14 | Resize 50% (window, Enter) | ✅ | 2400×1600 → 1200×800 | 12.1s |
| 15 | Add BG (window, Enter) | ✅ | 800×533 → 992×725 | 13.1s |
| 16 | Metadata: remove all (Enter) | ✅ | 0 metadata fields left | 12.4s |