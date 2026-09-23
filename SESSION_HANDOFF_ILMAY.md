# Oturum Özeti — (yeni sohbete devam için)

Bu dosya en son Claude Code oturumunda yapılan işleri özetliyor. Yeni sohbete bu dosyayı okutup
kaldığın yerden devam edebilirsin. **Önceki oturumlar altta, "Önceki oturumlar" başlığı altında
arşivlendi.**

## Şu an neredeyiz

- **Branch**: `feat/external-zone`.
- **Git durumu**: hiçbir şey commit edilmedi bu oturumda — kullanıcının varsayılan tercihi
  (önceki oturumlardan) "commit'i kendisi atmak istiyor". `git status -s` ile stage edilmemiş
  şu değişiklikler var:
  - Değişen: `README.md`, `backend/agent/composer.py`, `backend/agent/pipeline.py`,
    `backend/agent/router.py`, `backend/core/config.py`, `backend/extensions/web_tools/agent_protocol.py`,
    `backend/llm/client.py`, `backend/tools/external_series.py`, `frontend/src/App.jsx`,
    `frontend/vite.config.js`
  - Yeni: `Dockerfile`, `frontend/Dockerfile`, `docker-compose.yml`, `docker-compose.full.yml`,
    `.dockerignore`, `frontend/.dockerignore`
  - Takip edilmeyen (muhtemelen zararsız): `.DS_Store`, `docs_presentation/KKB Hackathon 2026 Kick
    Off Sunum...pdf`
- **Docker ile tüm sistem (backend+frontend+web arama+araştırma) çalışır durumda**, container'lar
  ayakta: `docker compose -f docker-compose.full.yml up --build` ile başlatıldı, hepsi healthy.
  `python extensions/web_tools/web-tools setup` bu oturumda çalıştırıldı (secret üretildi,
  `extensions/web_tools/.env`'de duruyor).
- Native (Docker'sız) backend/frontend süreçleri bu oturumda **durduruldu** (Docker ile
  çakışmasın diye) — tekrar native moda dönmek istenirse `docker compose down` sonra eskisi gibi
  `uvicorn`/`npm run dev`.

## PDF'ten (KKB Hackathon 2026 Kick Off Sunumu) öğrenilenler

- **Docker/deploy zorunlu değilmiş** — kullanıcı sözlü olarak "deploy lazım değil dediler" dedi,
  bu PDF'in "canlıda deploy edilmiş çözüm" ifadesiyle çelişiyor ama muhtemelen sonradan
  mentorlukta netleştirilmiş, güncel bilgi bu.
- **Web Search Tool + Web URL Agent Tool brief'te AÇIKÇA istenen 6 araçtan ikisi** (diğerleri:
  Lakehouse, Anomali, Causality, Change Detection) — opsiyonel değil, "Agentic Analytics Motoru"
  slaytında aynı listede. Demo öncesi `WEB_TOOLS_ENABLED=true` ile açık bırakılmalı.
- Kod tamamen private repo'da kalacak, KKB'ye GitHub erişimi verilerek teslim edilecek.
- Bulut/üçüncü taraf LLM API'leri kesin yasak, sadece Kloudeks.

## Bu oturumda bulunan ve düzeltilen bug'lar

### 1. "İlk N ay" tarih ayrıştırma — genitif eki bug'ı
`backend/agent/router.py` `DATE_TOKEN` — yılın iyelik eki SADECE tampon-"n" formlarını
(`'nın/'nin/'nun/'nün`) tanıyordu, tamponsuz formları (`'ın/'in/'un/'ün` — çoğu yıl bu grupta,
örn. "2024'ün") tanımıyordu. "2024'ün ilk 6 ayı" gibi sorular tüm yılı (12 ay) döndürüyordu.
**Fix**: `(?:['’]n[ıi]n)?` → `(?:['’]n?[ıiuü]n)?`.

### 2. Metadata sorularında composer kendi bulduğu seriyi inkâr ediyordu
`backend/agent/pipeline.py` + `backend/agent/composer.py` — "hangi veriler var" tipi sorularda
model bazen sıfır adımlı bir plan yazıyor (zaten metinle cevap veririm diye), `discover` adımı hiç
çalışmıyor, composer'ın önüne boş `facts` geliyor, "veri yok" diye uyduruyordu — halbuki plan'ın
kendi reasoning'i doğru seriyi (örn. TP.KTF12) buluyordu. **Fix**: metadata niyetli, veri
üretmeyen bir plana otomatik `discover` adımı ekleniyor (`apply_analysis`'ın deseniyle aynı);
composer'a kural #14 — `discovered_keys` doluysa bulunan serileri tanıt, "veri yok" deme.

### 3. Vade grubu (3 aya kadar/3 aydan fazla) toplama eksikti
`backend/agent/pipeline.py::repair_three_month_groups` — model TP/YP gibi çoklu para birimi
sorularında bazı vade kovalarını (örn. `bir_aya_kadar`) atlıyordu, eski kod TÜM kovalar zaten
fetch edilmiş olmadıkça hiç düzeltme yapmıyordu, ayrıca sadece İLK adresi düzeltip `break`
ediyordu (ikinci para birimi düzeltilmeden kalıyordu). **Fix**: eksik kovaları otomatik fetch
ediyor, TÜM adresleri (her para birimini) düzeltiyor, prefiks çakışmasını (`tl_`/`doviz_`) önlüyor.
Not (2026-09-23'te değişti): `vadesiz` artık "3 aya kadar" grubuna DAHİL. Gruplar satırın
tamamını paylaşır: kısa + uzun = toplam satırı, ve bu eşitliği bir test pinliyor. Eski okuma
("vadesiz = vade yok, iki gruba da girmez") kısa grubu vadesiz hareketi kadar eksik gösteriyordu
ve grupların toplamı satırı tutmuyordu.

### 4. Dış kaynak PDF'lerinde tarih ayrıştırma çöküyordu
`backend/tools/external_series.py::_parse_periods` — PDF tablosundaki alakasız bir metin parçası
("01-08" gibi) `dayfirst=True, format="mixed"` ile parse edilirken yıl-1 gibi imkânsız bir tarihe
düşüyordu, bu da `datetime64[ns]`'e atanırken pandas'ın kendi iç güvenlik kontrolünde
`AssertionError: Something has gone wrong...` diye çöküyordu — tek bir alakasız hücre yüzünden
PDF'in tamamı işlenemiyordu. **Fix**: `_assign_parsed_dates` yardımcı fonksiyonu, aralık-dışı
tarihleri NaT'a çeviriyor.

### 5. Web araştırması modunda "Web araştırması" seçeneği hep seçilebilirdi
`frontend/src/App.jsx` — backend `research_configured: false` dese bile dropdown'da seçenek hep
aktifti, kullanıcı seçip TEPKI ALAMADIKTAN SONRA altta küçük bir uyarı görüyordu. **Fix**: seçenek
`researchConfigured === false` iken `disabled`, etiketine "(kapalı)" ekleniyor.

### 6. DeepSeek-V4.1-Flash hibrit entegrasyonu
Kloudeks'e yeni eklenen `deepseek-ai/DeepSeek-V4.1-Flash` modeli test edildi: Qwen'e göre ÇOK hızlı
(0,1-0,4sn vs 10-90sn) ama **plan DSL'inin katı şemasını (extra="forbid") güvenilir şekilde
uygulamıyor** (uydurma alan ekliyor, zorunlu alan atlıyor — 3/3 testte). Küçük şemalar
(sınıflandırıcı, composer serbest metin) için güvenli. **Fix**: `KloudeksClient.chat`/`.structured`'a
per-call `model` override eklendi (`backend/llm/client.py`), `router.py`'nin sınıflandırıcısı ve
`composer.py`'nin prose üretimi artık `KLOUDEKS_FAST_MODEL` (DeepSeek) kullanıyor, **planner Qwen'de
kalıyor** (güvenli değil).

### 7. Composer, seri+dış kaynak birlikte sorulduğunda ilişki kurmuyordu
`backend/agent/composer.py` — "X verisini Y PDF'i ile yorumla" tarzı sorularda bazen iki bilgiyi
yan yana sayıp bırakıyordu, açık bir bağlantı cümlesi kurmuyordu (tutarsız, bazen kuruyordu bazen
kurmuyordu). **Fix**: kural #16 — series+documents birlikteyse açık bağlantı cümlesi zorunlu.

### 8. [extensions/web_tools, izinle düzeltildi] Araştırma modu her zaman 503/parse_error veriyordu
Docker'da "Web araştırması" (çok adımlı) modunu açmaya çalışırken art arda 4 ayrı config eksiği
bulundu ve düzeltildi (bkz. Docker bölümü altta), sonuncusu perhat'ın kodunda gerçek bir bug'dı:
`backend/extensions/web_tools/agent_protocol.py::model_decision` — her model kararı için
**sabit kodlanmış 50 saniyelik** bir timeout vardı (`min(50, asset_timeout_seconds - 2)`).
Context büyüdükçe (araştırma ilerledikçe, önceki bulgular prompt'a eklendikçe) model cevabı
yavaşlıyor, geç çağrılar 50sn'yi aşıp `model_timeout` ile başarısız oluyordu — ilk 2 adım (arama +
okuma) başarıyla çalışsa bile. **Fix**: kullanıcının açık izniyle `50` → `110` yükseltildi
(`asset_timeout_seconds - 2` sınırına daha yakın, hâlâ onun altında). **Doğrulandı**: aynı soru
(BDDK TBS raporu araştırması) artık `model_timeout` vermiyor — ama modelin kendi davranışından
kaynaklanan **ayrı, çözülmemiş bir `invalid_citation` hatası var** (model okumadığı kaynağa
referans veriyor / gerekli etiketi atlıyor) — bu bir sonraki oturumda araştırılabilir.

## Docker kurulumu (bu oturumda sıfırdan yapıldı)

- `Dockerfile` (backend), `frontend/Dockerfile`, `docker-compose.yml` (temel: backend+frontend,
  web arama varsayılan kapalı), `docker-compose.full.yml` (backend+frontend+SearXNG+crawler+egress,
  web arama VE araştırma modu varsayılan açık).
- İlk build **~400-420 saniye** (ölçüldü, kullanıcı kendi makinesinde 417,1sn ölçtü) — pip install +
  Playwright Chromium indirme + lakehouse build (tek başına ~48sn) + crawler'ın kendi Chromium+OCR
  kurulumu. Sonraki build'ler saniyeler (layer cache).
- **`docker-compose.full.yml` için bulunan 4 ayrı config eksiği** (hepsi düzeltildi):
  1. Crawler'ın kendi `WEB_AGENT_ENABLED` flag'i (backend'inkinden ayrı, `/health`'te
     `capabilities.agent` olarak raporlanıyor).
  2. Yanlış Docker build hedefi (`crawler` yerine `crawler-assets` olmalı — belge/görsel
     kütüphaneleri (pypdf, pdfplumber vb.) sadece `crawler-assets`'te kurulu, `readiness()`
     `agent_enabled=true` iken bunları zorunlu kılıyor).
  3. Crawler'ın kendi, backend'den AYRI üçüncü bir Kloudeks anahtarı (`WEB_KLOUDEKS_API_KEY`) —
     araştırma döngüsünün model çağrıları crawler container'ının İÇİNDE, kendi client'ıyla çalışıyor.
  4. Eksik volume mount (`asset-cache:/opt/web-tools-cache`) — crawler'ın dosya sistemi
     `read_only: true`, bu tek yol istisna. Eksikken `sqlite3.OperationalError: unable to open
     database file` → bu hata tipi `asset_worker.py`'nin özel yakaladığı tiplere uymuyor (OSError'dan
     türemiyor) → genel `except Exception:` bloğuna düşüp **yanıltıcı `parse_error`** ("dosya bozuk")
     mesajına dönüşüyordu. Gerçek hatayı bulmak için `agent_protocol.model_decision()`'ı subprocess/
     stderr-gizleme katmanını bypass ederek doğrudan çağırmak gerekti (worker'ın stdout/stderr'i
     `/dev/null`'a yönlendirilmiş, traceback normalde tamamen kayboluyor).
- README.md tamamen yeniden yazıldı: Docker en üstte/önerilen yol, native (Python/venv) yol ikinci
  sırada, her ikisi için de web arama/araştırma açma talimatları var, ~400sn build süresi notu var.

## Hâlâ açık olan, düzeltilmemiş bulgular

### A. Araştırma modunda `invalid_citation` hatası
Yukarıda #8'de bahsedildi — timeout düzeldi ama modelin kendi cevap formatı bazen kural dışı
(okumadığı kaynağa referans veriyor). Kod bug'ı değil, model davranışı — muhtemelen prompt/protokol
tarafında bir iyileştirme gerekiyor (`agent_protocol.py`'nin sistem promptu veya `validate_action`).

### B. `discover_concepts`'in ilk-6-cümlecik sınırı
Kullanıcı EVDS menü yolunu çok açık yazınca, "ve"lerle dolu gezinme metni `chunks[:6]` sınırını
dolduruyor, asıl konu cümleciği hiç aranmıyor. Muhtemel fix: uzunluğa göre seçmek.

### C. `FOLLOWUP_PATTERN` bazı ifadeleri tanımıyor (kısmen çözüldü)
Eski not hâlâ kısmen geçerli olabilir, bu oturumda tekrar test edilmedi.

### D. Tablo üretimi — kullanıcı bir soru sordu, netleşmedi
Kullanıcı "tablo demeden de tablo üretsin" dedi, örnek olarak "...bir veri seti oluştur." cümlesini
verdi — ama bu TAM CÜMLE zaten `TABLE_PATTERN` içindeki `veri\s*seti` ile eşleşiyor ve test edilince
(hem router hem canlı API) doğru şekilde `presentation.table: true` üretiyor. Kullanıcıya hangi
GERÇEK soru/senaryoda tablo gelmediği soruldu, cevap alınamadan oturum bitti — **sıradaki oturumda
netleştirilmeli**: (a) farklı bir cümle mi denendi, (b) backend doğru ama frontend'de mi görünmüyor.

## Genel notlar / hatırlatmalar

- **Bu oturumda kullanıcı Docker konusunda derin bir soru-cevap sürecinden geçti** — önce "docker
  hazırlasana" dedi, sonra "bunlar gerekli miydi" diye sorguladı (PDF'i okuyunca deploy'un aslında
  gerekmediği ortaya çıktı), sonunda Docker kurulumunu KABUL ETTİ ama native yolun da eksiksiz
  belgeli kalmasını istedi. Docker'ı "varsayılan/önerilen" ama native'i "tam alternatif" olarak
  sunmaya devam et.
- **`extensions/web_tools`'a (perhat'ın kodu) dokunmak için kullanıcı bu oturumda AÇIK İZİN VERDİ**
  ("perhatın kodunu değiştirme iznim var değiştir") — ama bu izin genel değil, spesifik olarak
  50sn timeout fix'i içindi. Başka bir değişiklik için tekrar sormak daha güvenli.
- Test soruları (kullanıcının verdiği demo senaryosu, sırasıyla):
  1. `BDDK Aylık Bülten verilerini kullanarak Tüketici Kredileri - Taşıt kredilerinin 2026 yılı ilk 6 ay verisi ile bir veri seti oluştur.`
  2. `Taşıt kredisi tutarlarına faiz oranlarını ekle. Veri setine aynı aylara denk gelecek şekilde EVDS, Taşıt Kredisi (TL, Stok, %) verisini ekle.`
  3. `Taşıt kredileri ve faiz oranlarını Pazar - Otomobil & Hafif Ticari linkinden "2026 Ağustos Otomobil ve Hafif Ticari Araç Pazar Değerlendirme" pdfindeki veriler ile yorumla.` (+ PDF linki: `https://www.odmd.org.tr/folders/2837/categorial1docs/6161/ODMD%20Bas%c4%b1n%20Bulteni%202%20Eyl%c3%bcl%202026.pdf`)
  4. `BDDK Aylık Bülten verilerini kullanarak 202101–202512 döneminde Türk parası (TP) ve yabancı para (YP) mevduatların gelişimini ve vade yapısını incelemek üzere aylık bir veri seti oluştur...` (tam metin dosyanın üstünde, fix #3'te)
  5. `BDDK Türk Bankacılık Sektörü Temel Göstergeleri raporunun resmi sayfasını bul, oku ve kapsamını kaynak göstererek açıkla.` (araştırma modu, frontend'in `RESEARCH_QUESTIONS` örnek chip'lerinden biri)
- Test suite: `.venv/bin/python -m pytest -q` → 911 geçti, 34 skip, 3 bilinen Docker-bağımlı test
  kırmızı (`test_cli.py` x2, `test_gateway.py::test_unavailable_worker_returns_503`) — gerçek
  regresyon değil, önceki oturumlardan da böyleydi.
- Docker container'ları durdurmak: `docker compose -f docker-compose.full.yml down` (volume'leri de
  silmek istersen `-v` ekle, ama `asset-cache`/`searxng-cache` silinirse ilk çalıştırmadaki gibi
  yeniden ısınması gerekir).

---

## Önceki oturumlar (integrate/graphs + ilk external-zone oturumu, artık merge edilmiş)

Bu bölüm, önceki oturumların özetlerinin birleşimi — referans için saklanıyor.

### Önceki external-zone oturumu — bulunan/düzeltilen 10 bug

1. Composer kendi eklediği sütunu inkâr ediyordu (kural #13 ile düzeltildi, bu oturumda kural #14/16
   olarak devam etti).
2. JS ile render edilen sayfalar okunamıyordu — `browser_render.py` eklendi.
3. Navigasyon menüsü karakter bütçesini yiyordu — `nav/header/footer` temizlendi.
4. Tek noktalı seriler grafikte görünmüyordu — `mode="markers"` fallback'i.
5. `discover_concepts` "ve" ile bölünen cümleciklerde kaynak filtresini kaybediyordu.
6. `extract_currency` "TL karşılığı" ifadesini yanlış yorumluyordu.
7. Rate+hacim karışık sorularda hacim serisi discovery'de görünmüyordu.
8. O fix'in "tutarlı/tutarsız" kelimelerinde yanlış tetiklenmesi.
9. Composer eksik ayları "-" ile doldurup uydurma tablo çiziyordu.
10. Akım/Stok discovery yanlılığı (+ bir regresyon ve düzeltmesi).

### integrate/graphs'ta bulunan 5 bug (+ 1 bonus) — daha da önceki oturum

1. Discovery skor çakışması — "Diğer Mevduat" "Toplam Mevduat"ı geçiyordu.
2. Executor'da province zorlaması yoktu.
3. Cümle bölme regex'i "Ankara'da"yı parçalıyordu.
4. Aynı turda iki il çekilince sütunlar birbirini eziyordu.
5. Dış dosya eklerken ISO tarihler bozuluyordu.
6. Bonus: "nedensellik iddia etme" dendiğinde bile causality planlanıyordu.

Detaylar için `git log`'da `integrate/graphs` commit'lerine (`37ce7a7`, `78dc3cb`, `6346746`,
`28ade2d`, `2ef4607`) bak.
