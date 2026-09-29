# Testing LOD2 Generation (`cascade_mesh`)

Panduan menguji port `lod_generation_v2` di `src/reconstruction_3d/lod2/` beserta glue
service-nya (`src/reconstruction_3d/cascade_mesh.py`). Isinya: hasil analisis port, lalu
lima level pengujian dari yang paling murah (unit test, detik) sampai yang paling mahal
(end-to-end lewat Docker dengan data nyata).

Helper script ada di [`docs/lod2-testing/`](lod2-testing/):

| File | Guna |
|---|---|
| `parity_check.py` | menjalankan referensi v2 **dan** port atas input yang sama, lalu membandingkan hasilnya byte demi byte |
| `mock_world.py` | mock gateway + object storage (GET signed URL, PUT presigned, POST callback). Hanya pakai stdlib |
| `make_request.py` | membuat `request.json` dan `estimate.json` untuk job `cascade_mesh` (CRS dibaca otomatis dari `dsm.tif`) |
| `prepare_real_data.py` | mengonversi shapefile BO/RS ke GeoJSON dan menyalin raster, dengan pemeriksaan kelengkapan dan CRS |
| `run_direct.py` | menjalankan pipeline langsung (tanpa HTTP) pada data nyata, mencetak ringkasan QA, opsional `--ref` untuk paritas |

Untuk pengujian dengan data asli, ikuti [TESTING_LOD2_REAL_DATA.md](TESTING_LOD2_REAL_DATA.md).

---

## 1. Hasil analisis: apakah port v2 sudah benar?

**Ya.** Algoritmanya diport dengan setia, dan ini sudah dibuktikan lewat eksekusi, bukan
hanya dengan membaca kode.

### 1.1 Peta modul

| Referensi (`sam-interactive-github/ai/lod_generation_v2/`) | Port (`src/reconstruction_3d/lod2/`) | Status |
|---|---|---|
| `interface.py` | `params.py` (`LOD2V2Params` → `LOD2Params`) | identik, minus `only_id`/`limit`/`bbox` |
| `_grid.py` | `grid.py` | identik, minus `world_coords()` yang tidak dipakai |
| `_io_vector.py` | `io_vector.py` | identik, minus kontrak 3D Viewer (lihat 1.2) |
| `_raster.py` | `raster.py` | identik (`inv * pt` → `inv @ pt`, sesuai affine 3.x) |
| `_partition.py` | `partition.py` | identik |
| `_plane_fit.py` | `plane_fit.py` | identik (parameter `params` yang tidak dipakai di `building_reference_z` dibuang) |
| `_reconcile.py` | `reconcile.py` | identik, minus `disagreement()` yang tidak dipakai |
| `_triangulate.py` | `triangulate.py` | identik, minus counter global `STATS` |
| `_solid.py` | `solid.py` | identik, minus `roof_height_range()` yang tidak dipakai |
| `_validate.py` | `validate.py` | identik, minus `check_attributes` (kontrak viewer) |
| `_cityjson11.py` | `cityjson.py` | identik |
| `core.py` | `core.py` | identik + `should_cancel` (lihat 1.2) |
| `runner_lod2_v2.py` (adapter Qt) | tidak diport | sengaja: service ini bukan PyQt |
| `__main__.py` (CLI + `--selftest`) | `tests/test_lod2.py` + `tests/lod2_fixtures.py` | selftest pelana menjadi test pytest |

### 1.2 Perbedaan yang disengaja

Semuanya konsisten dengan batas tanggung jawab service (lihat `CLAUDE.md`):

1. **Kontrak 3D Viewer Cascade3D dibuang.** Tidak ada lagi injeksi atribut `Id`,
   `check_attributes`, sanitasi id agar aman sebagai nama file, maupun batas 80 karakter. Output
   service ini dikonsumsi `naraga-converter`, bukan tab 3D Viewer. Kolom `level_0`/`level_1`
   tetap dibuat dari explode multipart. Atribut `Id` hanya muncul bila memang ada di BO.
2. **Knob seleksi CLI (`only_id`, `limit`, `bbox`) dibuang.** Job service selalu memproses
   seluruh tile.
3. **`should_cancel` ditambahkan** di `core.generate_lod2`. Callback ini dicek di antara
   bangunan, sehingga cancel atau timeout job benar-benar menghentikan worker thread.
   Referensi tidak membutuhkannya karena di sana Qt yang mengelola thread.
4. **Pesan error tidak memuat path file.** Di service, path tersebut adalah temp dir.
   Sementara itu, error download/upload tidak memuat signed URL karena URL itu adalah kredensial.
5. **Type hint lengkap (mypy strict).** Perubahan ini tidak mengubah perilaku.

### 1.3 Bukti eksekusi (dijalankan 2026-09-25)

| Pemeriksaan | Hasil |
|---|---|
| `uv run ruff check .` / `ruff format --check .` | bersih |
| `uv run mypy` (strict) | `Success: no issues found in 28 source files` |
| `uv run pytest` | **21 passed** (7 di antaranya khusus LOD2/cascade_mesh) |
| `--selftest` referensi di environment yang sama | `SELFTEST PASSED` (volume 2500,00 m³, bubungan 15,00, tritisan 10,00) |
| `parity_check.py`: 5 bangunan (pelana, limas, courtyard, tanpa RS, L bertingkat) | **QA report, `vertices`, `transform`, `metadata`, dan `geometry` identik** antara referensi dan port |
| Volume vs closed-form | pelana 2100,0 · limas 1200,0 · courtyard 4800,0 · kotak tanpa RS 256,0 m³, semuanya tepat |
| End-to-end HTTP native (service + `mock_world.py`) | callback seq 1→4 (`processing` 0/10/90 → `complete` 100), PUT CityJSON 1.1, replay → 409, tanpa auth → 401 |

Versi library saat diuji: shapely 2.1.2, geopandas 1.1.4, rasterio 1.5.1, numpy 2.5.2,
affine 3.0.1, fiona 1.10.1, pyproj 3.7.2.

### 1.4 Temuan yang perlu ditindaklanjuti

| # | Temuan | Dampak | Saran |
|---|---|---|---|
| 1 | ~~**`Dockerfile.dev` memasang `pip install -e .` tanpa `[geo]`**, sementara `Dockerfile` produksi memasang `'.[geo]'`~~ **Diperbaiki 2026-09-25**: `Dockerfile.dev` sekarang memasang `-e '.[geo]'` | Sebelum perbaikan, di `docker compose up` setiap job `cascade_mesh` gagal dengan `ModuleNotFoundError: geopandas` | terverifikasi: image dev ter-build dan geopandas/rasterio/shapely terpasang di container |
| 2 | ~~Data nyata belum pernah dijalankan lewat port~~ **Sebagian teratasi**: data Surabaya (153 bangunan) sudah lulus, geometri identik dengan referensi. Tile DKI **belum**, karena folder `data/3d_recon/lod - dki` hanya berisi dua file `.dbf` | Kasus tile DKI (323 bangunan, target 6.235 vertex) belum teruji | lengkapi folder DKI lalu ulangi [TESTING_LOD2_REAL_DATA.md](TESTING_LOD2_REAL_DATA.md) |
| 3 | pytest hanya menguji satu bangunan pelana. Limas, courtyard, bangunan tanpa RS, dan multi-bangunan hanya tercakup oleh `parity_check.py` (manual, butuh clone referensi) | regresi pada kasus itu tidak tertangkap CI | pindahkan skenario closed-form dari `parity_check.py` ke `tests/test_lod2.py` (tanpa bagian referensi) |
| 4 | ~~Job tetap `complete` walau ada bangunan yang tidak watertight (`on_invalid="warn"`)~~ **Diperbaiki (review PR #6)**: service memakai `on_invalid="skip"`, dan job gagal bila lebih dari separuh bangunan dikeluarkan (`MAX_SKIPPED_FRACTION`) | jumlah bangunan yang dikeluarkan masih hanya di-log, tidak dikirim ke gateway | laporkan jumlahnya begitu kontrak punya tempat untuk itu |
| 5 | `PendingDeprecationWarning` (`Use @ matmul`) saat pytest | tidak ada. Asalnya dari internal rasterio 1.5 dengan affine 3.x, bukan dari kode kita (port sudah memakai `@`) | abaikan |
| 6 | Komentar extra `geo` di `pyproject.toml` masih menyebut `ai/lod_generation` (v1) | kosmetik | ganti menjadi `lod_generation_v2` |

---

## 2. Prasyarat

```bash
uv sync --extra geo          # geopandas, rasterio, shapely, fiona, pyproj
```

**Wajib di mesin ini:** unset `PROJ_LIB`/`GDAL_DATA` peninggalan PostGIS sebelum menjalankan
apa pun di luar pytest (pytest sudah menanganinya lewat `conftest.py`). Kalau tidak, rasterio
membaca `proj.db` versi lama dan semua lookup CRS gagal.

```bash
# Git Bash
unset PROJ_LIB GDAL_DATA
```
```powershell
# PowerShell
Remove-Item Env:PROJ_LIB, Env:GDAL_DATA -ErrorAction SilentlyContinue
```

Semua perintah di bawah dijalankan dari akar repo.

---

## 3. Level 1: automated test (±35 detik)

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

Hanya bagian LOD2:

```bash
uv run pytest tests/test_lod2.py tests/test_cascade_mesh.py -v
```

| Test | Yang dibuktikan |
|---|---|
| `test_lod2.py::test_gable_matches_closed_form` | pelana sintetis watertight, bubungan 15 m, tritisan 10 m, tanah 0 m, volume 2500 m³ ±1%, progress monoton berakhir di 98 |
| `test_lod2.py::test_writes_cityjson_11_solid` | output CityJSON 1.1, ada `transform`, `referenceSystem` format OGC URL, `Building` → `Solid`, `lod == "2"`, tiga semantic surface |
| `test_lod2.py::test_inputs_are_never_modified` | file BO/RS/DSM/DTM tidak berubah satu byte pun (cacat utama v1) |
| `test_lod2.py::test_should_cancel_stops_the_run` | `should_cancel` menghentikan run dengan `LOD2Cancelled` dan tidak ada file output |
| `test_cascade_mesh.py::test_cascade_mesh_end_to_end` | lewat HTTP: submit → download → LOD2 → PUT → callback `complete`. `storage_key` berada di bawah `output_prefix`, `size_bytes` = ukuran body PUT, `result_summary is None` |
| `test_cascade_mesh.py::test_cascade_mesh_without_roof_structure_fails_clearly` | tanpa `remote_sensing` (RS), job `failed` dengan pesan yang menyebut key itu, dan tidak ada upload |
| `test_cascade_mesh.py::test_download_failure_never_leaks_signed_url` | download gagal → `failed`, dan pesan error tidak memuat query signature |

Test LOD2 otomatis di-*skip* (bukan gagal) kalau extra `geo` belum dipasang. Kalau
outputnya menunjukkan `skipped`, jalankan `uv sync --extra geo`.

---

## 4. Level 2: paritas terhadap referensi (±15 detik)

Level ini adalah bukti terkuat bahwa port setia. Butuh clone referensi di
`sam-interactive-github/` (read-only).

**a. Selftest bawaan referensi**, untuk memastikan referensinya sendiri sehat di environment ini:

```bash
cd sam-interactive-github
uv run --project .. python -W ignore -m ai.lod_generation_v2 --selftest
cd ..
```

Hasil yang diharapkan: `SELFTEST PASSED`.

**b. Paritas referensi vs port:**

```bash
uv run python -W ignore docs/lod2-testing/parity_check.py
```

Hasil yang diharapkan (log `INFO` disembunyikan):

```
REF : 5/5 buildings, 59 vertices, 0 not watertight, 1 flat fallback, 0 skipped
PORT: 5/5 buildings, 59 vertices, 0 not watertight, 1 flat fallback, 0 skipped
  gable      watertight=True faces=2 src=segmented roof=10.00..15.00 ground=2.00 vol=2100.0 dz=0.00
  hip        watertight=True faces=4 src=segmented roof=9.00..13.00 ground=2.00 vol=1200.0 dz=0.00
  courtyard  watertight=True faces=1 src=segmented roof=8.00..8.00 ground=2.00 vol=4800.0 dz=0.00
  no_rs      watertight=True faces=1 src=fallback_flat roof=6.00..6.00 ground=2.00 vol=256.0 dz=0.00
  l_step     watertight=True faces=2 src=segmented roof=6.00..9.00 ground=2.00 vol=1501.4 dz=3.00

PARITY PASSED: report, vertices, transform, metadata and geometry identical
```

Cara membaca hasilnya:

- **`hip`**: empat bidang segitiga bertemu di satu apex. Ini menguji rekonsiliasi z di vertex
  4-bidang.
- **`courtyard`**: bangunan bergenus 1. Dinding courtyard harus berada di shell eksterior
  (`boundaries[0]`), bukan sebagai shell kedua.
- **`no_rs`**: tidak ada segmen RS sama sekali, sehingga hasilnya kotak datar
  (`roof_source=fallback_flat`) yang tetap watertight.
- **`l_step`**: dua bidang datar 9 m dan 6 m. Peringatan `roof planes disagree by 3.00 m`
  memang **diharapkan**: rekonsiliasi merata-ratakan undakan di tepi bersama sehingga
  volumenya 1501,4, bukan 1504. Ini batasan #2 yang didokumentasikan di README referensi.

Exit code 0 berarti lulus, 1 berarti gagal (daftar penyebabnya dicetak). Folder `workdir`
menyimpan `ref.json` dan `port.json` untuk diinspeksi.

---

## 5. Level 3: pipeline di data nyata (menit)

Level ini menguji pipeline langsung, tanpa HTTP. **Sudah dijalankan pada data Surabaya**
(153 bangunan, lulus, paritas dengan referensi); panduan lengkap langkah demi langkah ada di
[TESTING_LOD2_REAL_DATA.md](TESTING_LOD2_REAL_DATA.md). Tile DKI di bawah ini belum
dijalankan (temuan #2).
Contoh di bawah memakai tile DKI referensi, tetapi data apa pun bisa dipakai asalkan BO, RS,
DSM, dan DTM satu CRS.

Catatan: `generate_lod2` bisa membaca `.shp` secara langsung, tetapi `cascade_mesh` via HTTP
saat ini hanya menerima GeoJSON (lihat To-do di `CLAUDE.md`).

**a. Jalankan port dan simpan QA report:**

```bash
uv run python -W ignore - <<'EOF'
import json, logging
from reconstruction_3d.lod2 import LOD2Params, generate_lod2
logging.basicConfig(level=logging.WARNING)
D = "path/ke/lod - dki"          # ganti
report = generate_lod2(
    LOD2Params(
        input_building=f"{D}/1_BO_DKI.shp",
        input_roof=f"{D}/1_RS_DKI.shp",
        input_dsm=f"{D}/DSM_48S_Clipped.tif",
        input_dtm=f"{D}/DTM_48S_Clipped.tif",
        output_file="out/port_dki.city.json",
    ),
    lambda msg, pct: print(f"[{pct:3d}%] {msg}"),
)
print(report.summary, f"{report.elapsed_seconds:.1f}s")
json.dump(report.to_dict(), open("out/port_report.json", "w"), indent=2)
EOF
```

**b. Bandingkan dengan angka yang dilaporkan README referensi** untuk tile DKI:

| Metrik | Target (README v2) | Sumber di QA report |
|---|---|---|
| Bangunan watertight | 323 dari 323 | `n_buildings_out`, `n_not_watertight == 0` |
| Jumlah vertex | 6.235 | `n_vertices` |
| Fallback datar (tanpa RS) | 30 | `n_fallback_flat` |
| Ground clamp (DTM ≥ DSM) | 7 | `n_ground_clamped` |
| `max_z_disagreement` > 1 m | 28 bangunan, maks. ±10,67 m | `buildings[].max_z_disagreement` |
| Waktu proses | ±17 detik | `elapsed_seconds` |

**c. Paritas di data nyata (disarankan).** Jalankan CLI referensi atas data yang sama, lalu
bandingkan per bangunan:

```bash
cd sam-interactive-github
uv run --project .. python -W ignore -m ai.lod_generation_v2 \
  --bo "$D/1_BO_DKI.shp" --rs "$D/1_RS_DKI.shp" \
  --dsm "$D/DSM_48S_Clipped.tif" --dtm "$D/DTM_48S_Clipped.tif" \
  --out ../out/ref_dki.json --report ../out/ref_report.json
cd ..
uv run python - <<'EOF'
import json
ref = json.load(open("out/ref_report.json")); port = json.load(open("out/port_report.json"))
for r in (ref, port): r.pop("elapsed_seconds"); r.pop("output_file")
same = [a == b for a, b in zip(ref["buildings"], port["buildings"])]
print("summary identical:", {k: v for k, v in ref.items() if k != "buildings"}
      == {k: v for k, v in port.items() if k != "buildings"})
print(f"per-building identical: {sum(same)}/{len(same)}")
EOF
```

**Syarat id:** paritas per bangunan hanya bermakna kalau BO sudah punya kolom `uuid_bgn`.
Tanpa kolom itu, kedua implementasi membuat uuid acak sehingga `object_id` berbeda. Tile DKI
yang pernah diproses v1 sudah memiliki `uuid_bgn`.

**d. Inspeksi visual** `out/port_dki.city.json` di [ninja.cityjson.org](https://ninja.cityjson.org)
(drag and drop). Yang dicari: atap tidak ganda, tidak ada lubang di sambungan atap-dinding, dan
dinding courtyard ada. Apex atap limas yang sedikit membulat adalah batasan #1 yang sudah
didokumentasikan, bukan bug.

**e. (Opsional) validasi skema** dengan cjio. Langkah ini belum diverifikasi di environment ini:

```bash
uvx --from cjio cjio out/port_dki.city.json validate
```

---

## 6. Level 4: end-to-end HTTP, service native (±1 menit)

Level ini menguji jalur service lengkap: auth, validasi input, download signed URL,
heartbeat, PUT, dan callback. `mock_world.py` berperan sebagai gateway sekaligus object
storage. Prosedur ini **sudah dijalankan dan lulus** dengan input sintetis.

Buka tiga terminal (semuanya dari akar repo, dengan `PROJ_LIB`/`GDAL_DATA` sudah di-unset).

**Terminal 1: siapkan input dan request.**

```bash
# input sintetis (pelana)...
uv run python -W ignore docs/lod2-testing/make_request.py out/e2e http://localhost:9000 --synthetic
# ...atau data sendiri: taruh bo.geojson, rs.geojson, dsm.tif, dtm.tif di out/e2e,
# sesuaikan konstanta EPSG di make_request.py, lalu jalankan tanpa --synthetic
```

Perintah ini mencetak `job_id` dan menulis `out/e2e/request.json` serta `out/e2e/estimate.json`.

**Terminal 2: mock gateway + storage.**

```bash
uv run python docs/lod2-testing/mock_world.py out/e2e 9000
```

**Terminal 3: service.**

```bash
INTERNAL_SERVICE_TOKEN=dev-internal-token STATE_DB_PATH=out/e2e-state.db \
  uv run uvicorn reconstruction_3d.main:app --app-dir src --port 8084
```

**Terminal 1: jalankan skenario.**

```bash
P=http://localhost:8084/v1/internal/reconstruction-3d
A="Authorization: Bearer dev-internal-token"
J='Content-Type: application/json'
JOB=$(uv run python -c "import json;print(json.load(open('out/e2e/request.json'))['job_id'])")

curl -s localhost:8084/health                                   # {"status":"ok"}
curl -s localhost:8084/ready                                    # {"status":"ok","checks":{}}
curl -s -H "$A" $P/capabilities                                 # models berisi "cascade_mesh"
curl -s -H "$A" -H "$J" -d @out/e2e/estimate.json $P/estimate   # credits_estimated >= 1
curl -s -w ' [%{http_code}]\n' -H "$A" -H "$J" -d @out/e2e/request.json $P/jobs   # [202]
curl -s -w ' [%{http_code}]\n' -H "$A" -H "$J" -d @out/e2e/request.json $P/jobs   # [409] idempotent
curl -s -H "$A" $P/jobs/$JOB/status                             # ... "complete", 100
curl -s -o /dev/null -w '%{http_code}\n' $P/capabilities        # 401 tanpa token
```

**Yang diharapkan di Terminal 2** (input sintetis):

```
CALLBACK seq=1 status=processing progress=0 error=None
CALLBACK seq=2 status=processing progress=10 error=None
CALLBACK seq=3 status=processing progress=90 error=None
PUT  output.city.json  982 bytes
CALLBACK seq=4 status=complete progress=100 error=None
```

Untuk data besar akan ada lebih banyak callback `processing` (heartbeat paling lambat tiap
`min(heartbeat_interval_seconds/2, 5)` detik) dengan progress naik di rentang 10..88.

**Periksa hasilnya:**

- `out/e2e/output.city.json`: `"version": "1.1"`, `metadata.referenceSystem` sesuai CRS input.
- Baris terakhir `out/e2e/callbacks.jsonl`: `output_datasets[0].storage_key` diawali
  `jobs/<job_id>/outputs/`, `size_bytes` = ukuran file output, `dataset_role = "mesh"`,
  `dataset_format` = format slot upload (`gltf`, karena kontrak belum punya `cityjson`), dan
  `result_summary = null`.

**Skenario negatif** (edit `out/e2e/request.json`, ganti `job_id` dengan UUID baru, lalu
submit ulang). Keempat baris pertama sudah diverifikasi: semuanya diterima dengan 202 lalu
berakhir `failed` dengan pesan seperti di tabel. Baris DELETE belum dijalankan manual, tetapi
aturan no-callback-after-cancel sudah dicakup `tests/test_jobs.py`.

| Ubah | Callback terminal yang diharapkan |
|---|---|
| hapus `input_datasets.remote_sensing` | `failed`, pesan menyebut `remote_sensing` |
| `params.elevation_source` → `"pointcloud"` | `failed`, pesan menyebut `dtm_dsm` |
| `input_datasets.dsm.dataset_format` → `"las"` | `failed`, `input 'dsm' is las; ...` |
| `input_datasets.dsm.signed_url` → `.../get/xxx?sig=rahasia` | `failed`, `download of input 'dsm' failed: HTTP 403`, **tidak** memuat `rahasia` |
| `DELETE $P/jobs/$JOB` saat job berjalan (butuh data besar) | tidak ada callback apa pun lagi setelah cancel |

---

## 7. Level 5: end-to-end lewat Docker (jalur yang didukung)

`Dockerfile.dev` sudah memasang extra `[geo]` (temuan #1). Kalau image dev sudah pernah
di-build sebelum perbaikan itu, **wajib `--build`** agar layer pip dibangun ulang. Tanpanya,
image lama tanpa geopandas tetap dipakai dan job akan `failed` dengan `ModuleNotFoundError`.

```bash
cp .env.example .env            # INTERNAL_SERVICE_TOKEN=dev-internal-token
docker compose up --build
```

Ulangi Level 4, tetapi service dijalankan oleh Docker (lewati Terminal 3) dan URL yang ditulis
ke request harus bisa dijangkau **dari dalam container**:

```bash
uv run python -W ignore docs/lod2-testing/make_request.py out/e2e http://host.docker.internal:9000 --synthetic
```

Docker Desktop di Windows sudah menyediakan `host.docker.internal`. Di Linux, tambahkan
`extra_hosts: ["host.docker.internal:host-gateway"]` pada service di `docker-compose.yml`.
Selama job berjalan, pastikan `/health` tetap merespons cepat (event loop tidak terblokir):

```bash
while true; do curl -s -o /dev/null -w '%{time_total}\n' localhost:8084/health; sleep 1; done
```

Uji restart juga: `docker compose restart` di tengah job. Job itu harus dilaporkan `failed`
saat startup (orphan recovery), bukan menggantung.

---

## 8. Checklist sebelum menyatakan LOD2 siap

- [x] Level 1: ruff, mypy, dan pytest hijau
- [x] Level 2: `SELFTEST PASSED` dan `PARITY PASSED`
- [x] Level 3 (data Surabaya): 153/153 bangunan, paritas geometri 153/153 dengan referensi
- [ ] Level 3 (tile DKI): 323/323 watertight, 6.235 vertex, paritas per bangunan. Data belum lengkap
- [x] Level 4: callback `complete`, 409 saat replay, 401 tanpa token, skenario negatif sesuai tabel
- [x] Temuan #1 diperbaiki (`Dockerfile.dev` memasang `[geo]`) dan terverifikasi lewat build Docker
- [x] Level 5 (data Surabaya): job `complete` lewat `docker compose up`, `/health` responsif selama job, orphan recovery

---

## 9. Troubleshooting

| Gejala | Penyebab | Solusi |
|---|---|---|
| `DATABASE.LAYOUT.VERSION.MINOR` / CRS gagal di-lookup | `PROJ_LIB`/`GDAL_DATA` milik PostGIS | unset (lihat bagian 2) |
| test LOD2 `skipped` | extra `geo` belum terpasang | `uv sync --extra geo` |
| `ModuleNotFoundError: geopandas` di callback `failed` (Docker) | image dev lama, di-build sebelum perbaikan temuan #1 | `docker compose up --build` |
| `inputs are in different reference systems` | BO/RS/DSM/DTM beda EPSG | reproject ke satu CRS |
| GeoJSON terbaca sebagai lon/lat | GeoJSON tanpa member `crs` legacy | tulis `"crs": {"type":"name","properties":{"name":"urn:ogc:def:crs:EPSG::<kode>"}}` atau pakai data yang sudah ber-CRS proyeksi |
| banyak `roof_source=fallback_flat` | RS tidak overlap dengan BO, atau segmen < `min_segment_area` (1 m²) | cek overlay BO vs RS di QGIS |
| `n_ground_clamped` tinggi | DTM di atas DSM di tepi bangunan | kualitas DTM, bukan bug kode |
| `roof planes disagree by X m` | segmentasi RS dan DSM tidak sepakat (atap bertingkat, RS kasar) | angka kualitas, bukan error. Periksa bangunan dengan `max_z_disagreement` terbesar |
| callback berhenti di `processing` lalu `failed` karena timeout | tile terlalu besar untuk `max_job_duration_seconds` | naikkan durasi di request, atau pecah tile |
