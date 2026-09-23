# MIA · Hackathon Rehberi

**API adresi:** https://mia.csp.kloudeks.com  
**Python istemcisinde `base_url`:** `https://mia.csp.kloudeks.com/v1`

| Model kimliği | Ne için kullanılır? | Endpoint |
|---|---|---|
| `kkbhackathon2026/Qwen3.8-27B` | Soru-cevap, özetleme, metin üretimi ve görsel yorumlama | `/v1/chat/completions` |
| `kkbhackathon2026/Qwen3-Embedding-8B` | Anlamsal arama ve benzerlik için metin vektörleri | `/v1/embeddings` |
| `kkbhackathon2026/Unlimited-OCR` | Belge görselinden metin çıkarma | `/v1/chat/completions` |

Model kimliklerini `kkbhackathon2026/` öneki ve büyük/küçük harfleriyle **aynen** kullanın.

Örneklerde `API_KEYINIZ` yerine size verilen MIA API anahtarını yazın.

## API anahtarınızı gizli tutun

- **Anahtarınız şifreniz gibidir.** Ekran görüntülerinde, sunumlarda, sohbetlerde veya uygulamanızın tarayıcı kodunda paylaşmayın.
- Örneklerde anahtarı doğrudan yazmak yalnızca yerel denemeyi kolaylaştırır. **Gerçek anahtar içeren dosyaları Git'e commit etmeyin, GitHub/GitLab'a push'lamayın; özel repolarda da paylaşmayın.** Kodu paylaşmadan önce anahtarı tekrar `API_KEYINIZ` ile değiştirin.
- Projenizde anahtarı ortam değişkeninde tutun. `.env` dosyası kullanıyorsanız `.gitignore` içine ekleyin ve commit öncesinde değişiklikleri kontrol edin.

## 1. Metin ve görsel · Qwen3.8-27B

Qwen3.8-27B, metnin yanında **görsel de alabilir**. Bir prompt'ta **en fazla 5 görsel** gönderebilirsiniz. Aşağıdaki örnek yalnızca metin gönderir; görsel göndermek için OCR örneğindeki gibi base64 kodlanmış `image_url` içerik blokları kullanın.

**`sohbet.py`** olarak kaydedin. `content` içindeki örnek verileri ve soruyu değiştirebilirsiniz.

```python
from openai import OpenAI

client = OpenAI(
    api_key="API_KEYINIZ",
    base_url="https://mia.csp.kloudeks.com/v1",
)

response = client.chat.completions.create(
    model="kkbhackathon2026/Qwen3.8-27B",
    messages=[{
        "role": "user",
        "content": "Örnek satış verisi: Ocak 100, Şubat 120, Mart 90 adet. Eğilimi kısaca yorumla.",
    }],
)

print(response.choices[0].message.content)
```

Çalıştırın: `python sohbet.py`. Yanıt terminale metin olarak yazdırılır.

## 2. Metin vektörleri · Qwen3-Embedding-8B

**`embedding_ornegi.py`** olarak kaydedin. Bu model sohbet yanıtı yerine her metin için bir sayı listesi (vektör) döndürür. Vektörleri anlamsal arama veya benzer metinleri bulmak için kullanabilirsiniz.

```python
from openai import OpenAI

client = OpenAI(
    api_key="API_KEYINIZ",
    base_url="https://mia.csp.kloudeks.com/v1",
)

response = client.embeddings.create(
    model="kkbhackathon2026/Qwen3-Embedding-8B",
    input="Mart ayında satışlar azaldı.",
    encoding_format="float",
)

vector = response.data[0].embedding
print("Vektör boyutu:", len(vector))
print("İlk 5 değer:", vector[:5])
```

Çalıştırın: `python embedding_ornegi.py`. Metnin vektör boyutu ve ilk beş değeri görüntülenir.

## 3. Görselden metin · Unlimited-OCR

Unlimited-OCR, bir prompt'ta **en fazla 3 görsel** alabilir. Aşağıdaki örnek tek görsel gönderir.

Okutacağınız **PNG** görselini aynı klasöre **`belge.png`** adıyla koyun. Aşağıdaki kodu **`ocr_ornegi.py`** olarak kaydedin. PDF için önce sayfaları PNG'ye dönüştürüp her sayfayı ayrı gönderin.

```python
import base64
from openai import OpenAI

client = OpenAI(
    api_key="API_KEYINIZ",
    base_url="https://mia.csp.kloudeks.com/v1",
)

with open("belge.png", "rb") as file:
    image_data = base64.b64encode(file.read()).decode("utf-8")

response = client.chat.completions.create(
    model="kkbhackathon2026/Unlimited-OCR",
    messages=[{
        "role": "user",
        "content": [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{image_data}"},
            },
            {"type": "text", "text": "<image>\ndocument parsing"},
        ],
    }],
    max_tokens=8192,
    temperature=0.0,
    extra_body={
        "skip_special_tokens": False,
        "vllm_xargs": {"ngram_size": 35, "window_size": 128},
    },
)

print(response.choices[0].message.content)
```

Çalıştırın: `python ocr_ornegi.py`. Çıkarılan metin terminale yazdırılır; çıktıda yerleşim bilgisini taşıyan `<|ref|>` ve `<|det|>` etiketleri bulunabilir.

**OCR için:** `<image>\ndocument parsing` metnini ve `extra_body` ayarlarını koruyun. Görseli örnekteki gibi base64 ile gönderin; harici görsel URL'si kullanmayın.

Birden fazla görsel gönderirken her görsel için ayrı bir `image_url` bloğu ekleyin ve `vllm_xargs` içindeki `window_size` değerini `1024` yapın. Tek görsel için örnekteki `128` değerini kullanın.

## Takılırsanız

| Sorun | Kontrol edin |
|---|---|
| `401` / `403` | API anahtarınız geçerli mi, bu modele erişimi var mı? Model kimliğini önekiyle aynen kopyaladınız mı? |
| `429` | Hız veya kota sınırını kontrol edin; hata mesajına göre bekleyin ya da organizasyon ekibine danışın. |
| `ModuleNotFoundError` | `openai` paketini kodu çalıştırdığınız Python ortamına yükleyin. |
| `FileNotFoundError` | `belge.png` doğru klasörde mi? Komutu örnek dosyaların bulunduğu klasörde çalıştırın. |
| OCR boş yanıt veriyor | `<image>` başlangıcını ve `skip_special_tokens: False` ayarını kontrol edin. |
