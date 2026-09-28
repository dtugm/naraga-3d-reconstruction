# Testing LOD2 dengan Data Asli (Surabaya)

Langkah-langkah menguji `cascade_mesh` (LOD2) dengan data asli di `data/3d_recon/`, dari
persiapan data sampai job penuh lewat Docker. Melengkapi [TESTING_LOD2.md](TESTING_LOD2.md)
(Level 3 dan Level 5 di sana).

Semua angka di dokumen ini **diukur langsung** pada 2026-09-25, bukan estimasi.

---

## 1. Data yang tersedia

| Folder | Isi | Bisa dipakai? |
|---|---|---|
| `data/3d_recon/surabaya/` | Building Outline (shp), Roof Structure (shp), DSM.tif, DTM.tif, plus Orthophoto dan Point Cloud | **Ya**, lengkap |
| `data/3d_recon/lod - dki/` | hanya `1_BO_DKI.dbf` dan `1_RS_DKI.dbf` (tanpa `.shp`, `.shx`, `.prj`, maupun DSM/DTM) | **Tidak**, file tidak lengkap. Salin ulang seluruh folder, terutama `.shp`, `.shx`, `.prj`, dan kedua `.tif`. Setelah lengkap, semua langkah di bawah berlaku sama |

`data/` ada di `.gitignore`, jadi data dan hasil uji di dalamnya tidak ikut ter-commit.

**Ringkasan data Surabaya** (semuanya EPSG:32749, WGS84 / UTM 49S):

| Layer | Detail |
|---|---|
| Building Outline | 153 poligon, 23 di antaranya berlubang (courtyard), luas median 103 m², semua valid |
| Roof Structure | 349 poligon, 19 berlubang, semua valid, punya kolom `uuid_bgn` |
| DSM / DTM | 4818 × 3289 piksel, GSD 0,108 m, nodata −32767, masing-masing 83 MB |

Yang **tidak dipakai** oleh LOD2: `Orthophoto.tif` dan `Point Cloud.las` (itu untuk `dream3d`).

### Hal yang perlu diketahui sebelum mulai

1. **Jalur HTTP hanya menerima GeoJSON**, bukan shapefile (batasan di To-do `CLAUDE.md`).
   Untuk lewat Docker, shapefile harus dikonversi dulu. Langkah 2 mengerjakannya, dan sudah
   dibuktikan bahwa konversi ini tidak mengubah hasil: geometri identik dengan membaca
   shapefile langsung.
2. **Roof Structure dikirim sebagai key `remote_sensing`.** Kontrak belum punya key
   `roof_structure`. `make_request.py` sudah menanganinya.
3. Ada **kredit**: job ini diestimasi 2 kredit (1,59 unit × 100 MiB). Itu angka placeholder
   di `_estimate_credits`, bukan harga sungguhan.

---

## 2. Persiapan (sekali saja)

Dari akar repo. `PROJ_LIB`/`GDAL_DATA` peninggalan PostGIS harus di-unset, kalau tidak
rasterio gagal membaca CRS:

```bash
# Git Bash
unset PROJ_LIB GDAL_DATA
uv sync --extra geo
```
```powershell
# PowerShell
Remove-Item Env:PROJ_LIB, Env:GDAL_DATA -ErrorAction SilentlyContinue
uv sync --extra geo
```

Konversi shapefile → GeoJSON dan salin raster ke folder kerja `data/3d_recon/run/`:

```bash
uv run python -W ignore docs/lod2-testing/prepare_real_data.py data/3d_recon/surabaya data/3d_recon/run
```

Hasil yang diharapkan:

```
bo: 153 features, EPSG:32749, 23 interior rings, 0 invalid geometries
rs: 349 features, EPSG:32749, 19 interior rings, 0 invalid geometries
dsm: 4818x3289, GSD 0.108 m, EPSG:32749
dtm: 4818x3289, GSD 0.108 m, EPSG:32749

READY in data\3d_recon\run  (all inputs EPSG:32749)
```

Script ini keluar dengan kode 1 dan mencetak `NOT READY` bila ada file yang hilang, layer
kosong, `.prj` tidak ada, atau CRS antar-input berbeda. Perbaiki dulu sebelum lanjut.

Untuk data lain, tambahkan `--bo`, `--rs`, `--dsm`, `--dtm` dengan nama file-nya.

---

## 3. Langkah A: pipeline langsung (±10 detik, tanpa Docker)

Menguji algoritma LOD2 saja, tanpa HTTP. Jalankan ini **sebelum** Docker: kalau di sini
sudah bermasalah, masalahnya ada di data atau algoritma, bukan di service.

```bash
D=data/3d_recon/surabaya
uv run python -W ignore docs/lod2-testing/run_direct.py \
  "$D/Building Outline.shp" "$D/Roof Structure.shp" $D/DSM.tif $D/DTM.tif \
  data/3d_recon/run_direct --ref
```

`--ref` juga menjalankan referensi `lod_generation_v2` dan membandingkannya. Bagian itu butuh
clone `sam-interactive-github/` dan kolom `uuid_bgn` di outline (data Surabaya punya).
Hilangkan `--ref` bila tidak ingin membandingkan.

**Hasil yang diharapkan** (persis ini pada data Surabaya):

```
153/153 buildings, 2605 vertices, 1 not watertight, 11 flat fallback, 0 skipped  (7.4s, EPSG:32749)
ground clamped (DTM >= DSM): 16
buildings with a fallback plane: 30
roof_source: {'segmented': 142, 'fallback_flat': 11}
height m  p50 8.9  p90 22.0  max 82.6  min 0.7
roof-plane disagreement > 1 m: 24 buildings
flagged by validation: 1

REF : 153/153 buildings, 2605 vertices, 1 not watertight, 11 flat fallback, 0 skipped  (7.1s)
vertices identical: True (2605)
geometry identical: 153/153 buildings
PARITY PASSED
```

Output ada di `data/3d_recon/run_direct/`: `port.city.json` (hasil), `report.json` (QA per
bangunan), dan `ref.city.json` bila memakai `--ref`.

### Cara membaca hasilnya

| Metrik | Arti | Kapan curiga |
|---|---|---|
| `153/153 buildings` | semua bangunan menghasilkan solid | `n_skipped > 0` berarti ada yang gagal total. Lihat `problems` di `report.json` |
| `not watertight` | bangunan yang gagal salah satu pemeriksaan validasi | lihat penjelasan di bawah tabel ini |
| `fallback_flat` | bangunan tanpa segmen RS yang bisa dipakai, jadi berbentuk kotak beratap datar. Tetap watertight | proporsi besar berarti RS tidak menutupi sebagian besar outline |
| `ground clamped` | DTM ≥ DSM di tepi bangunan, tinggi dipaksa positif | angka tinggi berarti DTM buruk, bukan bug kode |
| `fallback plane` | ada bidang atap yang tidak bisa di-fit, jatuh ke bidang datar | bidang sempit (< ±4 piksel) memang begitu: batas data |
| `disagreement > 1 m` | segmentasi RS dan DSM tidak sepakat di suatu titik | angka kualitas paling berguna. Periksa bangunan teratas secara visual |
| `height max 82.6` | lihat catatan di bawah | tinggi tak wajar |

**"1 not watertight" tidak berarti ada lubang.** Flag `watertight` di QA report bernilai
`false` bila *salah satu* pemeriksaan gagal, termasuk uji kewajaran volume. Pada data ini,
bangunan `dcf3f52f…` tertutup secara topologi, tetapi volumenya hanya 0,25× prisma tapak
(batas bawah 0,4×). Penyebabnya: satu dari empat bidang atapnya gagal di-fit (RMSE 0,82 m,
jatuh ke fallback) dan bidang-bidangnya berselisih 3,7 m.

Langkah A memakai default library `on_invalid="warn"` (sama dengan referensi, agar paritas
bisa dibandingkan), sehingga bangunan ini tetap ditulis. **Service memakai `"skip"`**: lewat
HTTP/Docker (Langkah B) bangunan ini dikeluarkan, sehingga output berisi **152** bangunan
dan 2588 vertex. Job baru dianggap gagal bila lebih dari separuh bangunan dikeluarkan
(`MAX_SKIPPED_FRACTION` di `cascade_mesh.py`).

**Bangunan tertinggi (82,6 m, atap datar di z = 122,3 m, tanah 39,7 m).** RMSE bidangnya 0,0
dan tidak ada perselisihan, jadi konsisten dengan bangunan tinggi sungguhan atau objek dengan
puncak datar di DSM. DSM data ini mencapai 164,6 m sementara DTM hanya 55,6 m. Cek visual
(Langkah C) untuk memastikan itu bukan derau DSM. Kode LOD2 hanya mengikuti DSM.

---

## 4. Langkah B: end-to-end lewat Docker (±15 detik)

Menguji jalur service lengkap: auth, validasi input, download signed URL, heartbeat, upload,
callback, idempotensi, dan orphan recovery. `mock_world.py` berperan sebagai gateway dan
object storage, berjalan di host dan diakses container lewat `host.docker.internal`.

### B1. Nyalakan service

```bash
cp .env.example .env            # sekali saja; INTERNAL_SERVICE_TOKEN=dev-internal-token
docker compose up --build       # --build wajib bila image lama dibuat sebelum perbaikan [geo]
```

Pastikan siap dan library geo ada di dalam container:

```bash
curl -s localhost:8084/health           # {"status":"ok"}
docker exec naraga-3d-reconstruction-reconstruction-3d-1 \
  python -c "import geopandas, rasterio, shapely; print('geo ok')"
```

Bila baris kedua menghasilkan `ModuleNotFoundError`, image masih versi lama. Jalankan
`docker compose up --build` lagi.

### B2. Buat request dan nyalakan mock

Terminal 1, dari akar repo:

```bash
uv run python -W ignore docs/lod2-testing/make_request.py data/3d_recon/run http://host.docker.internal:9000
# -> job_id <uuid>  crs EPSG:32749
uv run python docs/lod2-testing/mock_world.py data/3d_recon/run 9000
```

`make_request.py` membaca CRS dari `dsm.tif`, jadi tidak perlu diedit untuk zona UTM lain.
Tiap kali dijalankan ia membuat `job_id` baru, sehingga job bisa diulang tanpa 409.

Windows Firewall mungkin menampilkan prompt untuk Python di port 9000. Izinkan, atau
container tidak akan bisa mengunduh input.

### B3. Kirim job

Terminal 2:

```bash
W=data/3d_recon/run
P=http://localhost:8084/v1/internal/reconstruction-3d
A="Authorization: Bearer dev-internal-token"; J='Content-Type: application/json'
JOB=$(python -c "import json;print(json.load(open('$W/request.json'))['job_id'])")

curl -s -H "$A" -H "$J" -d @$W/estimate.json $P/estimate                    # credits_estimated: 2
curl -s -w ' [%{http_code}]\n' -H "$A" -H "$J" -d @$W/request.json $P/jobs   # [202]
curl -s -H "$A" $P/jobs/$JOB/status                                         # ulangi sampai "complete"
curl -s -o /dev/null -w '%{http_code}\n' -H "$A" -H "$J" -d @$W/request.json $P/jobs   # 409
```

**Yang tampil di Terminal 1 (mock)** pada data Surabaya:

```
CALLBACK seq=1 status=processing progress=0
CALLBACK seq=2 status=processing progress=5
CALLBACK seq=3 status=processing progress=5
CALLBACK seq=4 status=processing progress=10
CALLBACK seq=5 status=processing progress=50
CALLBACK seq=6 status=processing progress=90
PUT  output.city.json  163784 bytes
CALLBACK seq=7 status=complete progress=100
```

`sequence` naik satu-satu dan `progress` tidak pernah turun. Jumlah callback `processing`
bergantung pada lama job (heartbeat paling lambat tiap `min(heartbeat_interval_seconds/2, 5)`
detik).

### B4. Periksa hasil

`data/3d_recon/run/output.city.json` adalah hasil yang di-upload, dan
`data/3d_recon/run/callbacks.jsonl` adalah log semua callback.

```bash
python - <<'EOF'
import json
W = "data/3d_recon/run"
out = json.load(open(f"{W}/output.city.json"))
last = [json.loads(l) for l in open(f"{W}/callbacks.jsonl")][-1]
print(out["version"], out["metadata"]["referenceSystem"], len(out["CityObjects"]), "buildings")
print(last["status"], last["credits_used"], last["result_summary"])
print(json.dumps(last["output_datasets"][0], indent=1))
EOF
```

Yang diharapkan:

- `1.1  http://www.opengis.net/def/crs/EPSG/0/32749  152 buildings`
- callback terminal: `complete`, `credits_used = 2`, `result_summary = null`
- `output_datasets[0]`: `storage_key` diawali `jobs/<job_id>/outputs/`, `size_bytes` sama dengan
  ukuran file (163784), `dataset_role = "mesh"`, `crs = "EPSG:32749"`,
  `dataset_format = "gltf"` (format slot upload: kontrak belum punya `cityjson`)

**Output lewat Docker sama dengan Langkah A dikurangi satu bangunan** yang gagal validasi
(`dcf3f52f…`), karena service memakai `on_invalid="skip"`. Hasilnya 152 bangunan dan 2588
vertex, tanpa vertex yatim. Untuk membandingkan byte demi byte, jalankan Langkah A dengan
`LOD2Params(..., on_invalid="skip")`. Bila berbeda, ada yang salah di jalur service
(download, upload, atau parameter).

### B5. Pastikan `/health` tidak terblokir selama job

Event loop tidak boleh macet, atau orchestrator mematikan container di tengah job. Di
terminal lain, saat job berjalan:

```bash
for i in $(seq 1 15); do curl -s -o /dev/null -w '%{time_total}s ' localhost:8084/health; sleep 1; done
```

Pada pengujian, waktu respons tetap ±0,2 detik selama job, dengan dua lonjakan singkat
(1,4 detik dan 1,0 detik) di sekitar awal job. Tidak ada yang macet. Job ini hanya berjalan ±10 detik, jadi uji yang lebih keras baru berarti
dengan tile yang jauh lebih besar.

### B6. Uji restart di tengah job (orphan recovery)

Buat job baru, lalu restart container 3 detik kemudian:

```bash
rm -f data/3d_recon/run/callbacks.jsonl
uv run python -W ignore docs/lod2-testing/make_request.py data/3d_recon/run http://host.docker.internal:9000
JOB=$(python -c "import json;print(json.load(open('data/3d_recon/run/request.json'))['job_id'])")
curl -s -o /dev/null -w 'submit %{http_code}\n' -H "$A" -H "$J" -d @data/3d_recon/run/request.json $P/jobs
sleep 3; docker restart naraga-3d-reconstruction-reconstruction-3d-1
curl -s -H "$A" $P/jobs/$JOB/status
```

Yang diharapkan: status berakhir `failed`, dan callback terakhir di `callbacks.jsonl` adalah
`failed` dengan pesan `service restarted while the job was running`. Job tidak boleh
menggantung di `processing`. State job ada di volume `state`, sehingga bertahan lewat restart.

### B7. Skenario negatif (opsional)

Ubah `request.json`, ganti `job_id` dengan UUID baru, lalu kirim ulang:

| Ubah | Hasil |
|---|---|
| hapus `input_datasets.remote_sensing` | `failed`: `cascade_mesh is missing input(s): remote_sensing …` |
| `params.elevation_source` → `"pointcloud"` | `failed`: `cascade_mesh requires params.elevation_source == 'dtm_dsm'` |
| `input_datasets.dsm.dataset_format` → `"las"` | `failed`: `input 'dsm' is las; cascade_mesh accepts geotiff, cog here` |
| `signed_url` DSM → `http://host.docker.internal:9000/get/xxx?sig=rahasia` | `failed`: `download of input 'dsm' failed: HTTP 403`, tanpa kata `rahasia` |

---

## 5. Langkah C: inspeksi visual

Tes otomatis membuktikan topologi tertutup, bukan bahwa model terlihat benar.

1. Buka [ninja.cityjson.org](https://ninja.cityjson.org) dan seret
   `data/3d_recon/run/output.city.json` ke halaman.
   Alternatif lokal: QGIS dengan plugin CityJSON Loader.
2. Yang dicek:
   - tidak ada atap ganda dan tidak ada lubang di sambungan atap-dinding;
   - dinding courtyard ada pada 23 bangunan berlubang;
   - bangunan `dcf3f52fe2ff4334aca886a4c041d73e` (volume tak wajar): **tidak ada** di output
     service (dikeluarkan karena `skip`). Lihat di `data/3d_recon/run_direct/port.city.json`
     dari Langkah A;
   - bangunan `a4312ef9…` (tertinggi, 82,6 m): apakah memang menara atau derau DSM;
   - lima bangunan dengan `max_z_disagreement` terbesar (lihat `report.json`), mis.
     `aaaabc3b…` (8,26 m).
3. Apex atap limas yang sedikit membulat adalah batasan #1 yang didokumentasikan di README
   referensi, bukan bug.

Untuk mencari bangunan tertentu di `report.json`:

```bash
python -c "
import json
r=json.load(open('data/3d_recon/run_direct/report.json'))
for b in sorted(r['buildings'], key=lambda b:-b['max_z_disagreement'])[:10]:
    print(b['object_id'], b['max_z_disagreement'], b['height'], b['roof_source'], b['problems'])"
```

**Validasi skema dengan cjio** belum pernah dijalankan di environment ini. Coba:
`uvx --from cjio cjio data/3d_recon/run/output.city.json validate`.

---

## 6. Checklist

- [x] Data lengkap dan satu CRS (`prepare_real_data.py` → `READY`)
- [x] Langkah A: 153/153 bangunan, 1 ditandai validasi (bukan lubang), 7,4 detik
- [x] Langkah A `--ref`: `PARITY PASSED`, geometri 153/153 bangunan identik dengan referensi v2
- [x] Langkah B: job `complete` lewat Docker, output identik dengan Langkah A, replay → 409
- [x] B5: `/health` tetap responsif selama job
- [x] B6: restart di tengah job → `failed` (bukan menggantung)
- [ ] Langkah C: inspeksi visual (butuh mata manusia)
- [ ] Validasi cjio
- [ ] Folder `lod - dki` dilengkapi dan diuji ulang (target README v2: 323/323 watertight, 6.235 vertex)

---

## 7. Yang perlu diketahui dari hasil ini

1. **Data nyata lolos pada percobaan pertama.** Tidak ada crash, tidak ada bangunan yang
   hilang, dan tidak ada perbedaan geometri dari referensi. Ini bukti terkuat bahwa port setia.
   Satu-satunya beda dari referensi adalah atribut `Id` yang hanya disuntikkan referensi
   (kontrak 3D Viewer Cascade3D, sengaja dibuang di port ini).
2. **Bangunan yang gagal validasi dikeluarkan dari output, dan job tetap `complete`**
   selama yang dikeluarkan tidak lebih dari separuh. Gateway tidak diberi tahu jumlahnya
   (`result_summary = null`). Hanya log service yang mencatatnya. Bila gateway perlu tahu,
   itu harus lewat kontrak (`dtugm/naraga-contract`).
3. **Angka "24 bangunan dengan disagreement > 1 m" (16%) lebih tinggi dari tile DKI**
   (28 dari 323 = 9%). Wajar untuk data yang berbeda, tetapi layak dilihat: bisa jadi RS
   Surabaya lebih kasar, atau atapnya lebih bertingkat. Periksa di Langkah C.
4. **Waktu 7,4 detik untuk 153 bangunan** (±20 bangunan/detik). Tile 5.000 bangunan
   diperkirakan ±4 menit, jauh di bawah `max_job_duration_seconds` default 10.800 detik.
   Ini ekstrapolasi kasar, bukan hasil ukur.

---

## 8. Membersihkan

Semua file uji ada di `data/3d_recon/` (di-ignore git):

```bash
rm -rf data/3d_recon/run data/3d_recon/run_direct     # ±170 MB, hasil salinan raster
docker compose down                                    # tambah -v untuk menghapus state job
```

`docker compose down -v` menghapus volume `state` berikut riwayat job dan outbox callback.

---

## 9. Troubleshooting

| Gejala | Penyebab | Solusi |
|---|---|---|
| `NOT READY` dari `prepare_real_data.py` | file hilang, `.prj` tidak ada, atau CRS beda | ikuti pesan yang dicetak |
| `DATABASE.LAYOUT.VERSION.MINOR` / CRS gagal dibaca | `PROJ_LIB`/`GDAL_DATA` milik PostGIS | unset (Bagian 2) |
| job `failed`: `ModuleNotFoundError: geopandas` | image dev lama | `docker compose up --build` |
| job `failed`: `download of input 'x' failed: ConnectError` | container tidak bisa menjangkau mock | pastikan `mock_world.py` jalan, izinkan Windows Firewall, dan base URL memakai `host.docker.internal`. Di Linux tambahkan `extra_hosts: ["host.docker.internal:host-gateway"]` |
| job `failed`: `download … HTTP 403` | nama file tidak dikenal mock | `mock_world.py` hanya menyajikan `bo.geojson`, `rs.geojson`, `dsm.tif`, `dtm.tif` dari folder kerja |
| `409` di submit pertama | `job_id` sudah pernah dikirim | jalankan ulang `make_request.py` (membuat `job_id` baru) |
| `401` | token salah | samakan dengan `INTERNAL_SERVICE_TOKEN` di `.env` |
| `inputs are in different reference systems` | BO/RS/DSM/DTM beda EPSG | reproject ke satu CRS sebelum `prepare_real_data.py` |
| banyak `fallback_flat` | RS tidak menutupi outline | cek overlay BO dan RS di QGIS |
| tile besar timeout | melebihi `max_job_duration_seconds` di request | naikkan nilainya, atau pecah tile |
