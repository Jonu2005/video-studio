# Video Studio ko FREE mein online karna (Hugging Face Spaces)

Ye folder poori website hai — isko Hugging Face Spaces par daalne se duniya
mein koi bhi aapki website khol kar video bana sakta hai. Hosting bilkul
free hai (koi card nahi chahiye). Domain aapko alag se khareedna hoga
(lagbhag ₹800–1000/saal) — wo baad mein bhi lag sakta hai.

Website ka flow: **Google se login → roz 3 videos muft banao → har video ka
30 second ka watermark preview dekho → poori Full HD video download ke liye
₹1 pay karo (Razorpay/UPI se).** Har user ki videos sirf usi ko dikhti hain.

## Part 1 — Space banao aur files daalo (lagbhag 15–20 minute)

1. **Hugging Face account banao (free):** https://huggingface.co/join
   par jao, email se sign up karo.
2. **Naya Space banao:** https://huggingface.co/new-space par jao.
   - Space name: `video-studio` (ya apni pasand ka naam)
   - SDK: **Docker** chuno (bahut zaroori — "Docker" hi chuno)
   - Space hardware: **CPU basic · 2 vCPU · 16GB · FREE** (default)
   - Visibility: Public
   - "Create Space" dabao.
3. **Is folder ki files upload karo:** Space ke page par "Files" tab →
   "Add file" → "Upload files" → is folder ki SAARI files aur folders
   (Dockerfile, README.md, DEPLOY.md, .env.example, engine, server,
   config.json, requirements.txt, music) select karo → Upload karo.
   (Git jaante ho to `git clone` karke push bhi kar sakte ho.)
   ZIP file (`video-studio-huggingface.zip`) upload karne ki zaroorat
   nahi hai — wo sirf backup ke liye hai.
4. **Build ka wait karo:** Space apne aap build hona shuru ho jayega.
   Pehli baar 10–15 minute lag sakte hain (ffmpeg, voice models
   download hote hain). "Building" se "Running" ho jaye to website live
   hai: `https://huggingface.co/spaces/<aapka-username>/video-studio`

## Part 2 — Secrets lagao (bina inke login/payment nahi chalega)

Space ke **Settings → Variables and secrets → New secret** mein ye
secrets daalo (naam bilkul aise hi likhna hai; `.env.example` file mein
poori list hai). Har secret daalne ke baad Space khud restart ho jata hai.

### A. Google login (zaroori — bina iske koi video nahi bana sakta)

1. https://console.cloud.google.com par jao (apne Google account se),
   naya project banao (naam: Video Studio).
2. **APIs & Services → OAuth consent screen** kholo: User type =
   **External**, app name = Video Studio, support email = apni email,
   baaki khaali chhod kar Save karo. (Publishing status "Testing" bhi
   chalega shuru mein, lekin tab sirf test users login kar payenge —
   sabke liye kholne ke liye baad mein "Publish App" karna hoga.)
3. **APIs & Services → Credentials → Create Credentials → OAuth client
   ID** → Application type = **Web application**.
4. **Authorized redirect URIs** mein ye daalo:
   `https://huggingface.co/spaces/<aapka-username>/video-studio/auth/callback`
   (agar baad mein custom domain lagao to uska bhi ek redirect URI add
   karna hoga: `https://<aapka-domain>/auth/callback`)
5. Create dabao — **Client ID** aur **Client Secret** milenge. Inhe
   Space secrets mein daalo:
   - `GOOGLE_CLIENT_ID` = Client ID
   - `GOOGLE_CLIENT_SECRET` = Client Secret
   - `OAUTH_REDIRECT_URI` = wahi redirect URI jo upar daala tha
6. `SESSION_SECRET` = koi bhi lamba random string (jaise 64 characters).
   Apne computer par bana lo:
   `python3 -c "import secrets; print(secrets.token_hex(32))"`
   Ise kisi se share mat karna.

### B. Razorpay payment (₹1 download ke liye)

1. https://razorpay.com par free account banao. **Pehle TEST mode se
   shuru karo** — test keys ke liye KYC nahi chahiye.
2. Dashboard → Settings → API Keys → **Test Mode** on karo → key
   generate karo. `Key ID` aur `Key Secret` milenge.
3. Space secrets mein daalo:
   - `RAZORPAY_KEY_ID` = test Key ID (rzp_test_... se shuru hoti hai)
   - `RAZORPAY_KEY_SECRET` = test Key Secret
4. **Live payment ke liye** (asli ₹1 lene ke liye): Razorpay mein KYC
   poora karna hoga (PAN + bank account — aapko khud karna hoga, ismein
   kuch din lagte hain). KYC ke baad live keys (rzp_live_...) milengi —
   unhe test keys ki jagah daal dena.
5. Bina Razorpay keys ke website chalti hai (video banana + preview),
   lekin Full HD download ka button "payment not set up yet" dikhayega.

### C. Stock footage (recommended — videos mein asli footage ke liye)

- `PIXABAY_API_KEY` = aapki Pixabay API key (pixabay.com par login
  karke API docs page par milti hai). Isse videos mein asli stock
  footage aati hai. Bina key ke bhi video banti hai (Wikimedia Commons
  + AI images), lekin footage kam milti hai.
- `PEXELS_API_KEY` = optional (Pexels ne nayi keys dena abhi rok rakha
  hai — jab dobara mile to daal sakte ho).

## Part 3 — Check karo

1. Website kholo → **Google se Login** dabao → Google account chuno →
   wapas website par aa jao. Login hona chahiye.
2. Chhota sa script (30–100 shabd) likh kar video banao. Free CPU par
   2-minute video banne mein lagbhag 5–10 minute lag sakte hain —
   normal hai.
3. Video banne ke baad **30 second ka watermark preview** dikhna
   chahiye, aur neeche **₹1 mein Full HD Download** ka button.
4. Test payment: test mode mein Razorpay ka test UPI/card use karke
   ₹1 pay karo (asli paise nahi katte) → payment ke baad Full HD
   download link khulna chahiye.

## Support email badalna

Legal pages (About, Privacy, Terms, Refund) mein `[SUPPORT_EMAIL]`
likha hai — ye ek placeholder hai. Apni support email daalne ke liye
`server/static/` ki chaaron HTML files mein `[SUPPORT_EMAIL]` ko apni
email se badal kar files dobara upload kar do.

## Apna domain lagana (baad mein, jab domain khareed lo)

1. Space ke "Settings" mein "Custom domain" mein apna domain daalo.
2. Domain wali website (jahan se khareeda) par DNS mein CNAME record
   banao jo aapke Space ki taraf point kare (Settings page par exact
   address likha hota hai).
3. **Google OAuth mein bhi naya redirect URI add karna hai:**
   Google Cloud Console → aapka OAuth client → Authorized redirect
   URIs mein `https://<aapka-domain>/auth/callback` add karo, aur Space
   secret `OAUTH_REDIRECT_URI` ko bhi naye domain wale URI se badal do.
   (Purana huggingface.co wala URI bhi rakha reh sakta hai.)
4. Thodi der mein aapki website apne domain par khulne lagegi.

## Jaankari (honest notes)

- **Free Space sota hai:** agar koi 48 ghante tak website na khole to
  Space sleep ho jata hai; agli visit par 1–2 minute mein jaag jata hai.
- **Video ki limit:** is free setup par ek video zyada se zyada lagbhag
  400 shabdon ki script (2–3 minute) tak banegi, aur ek user roz 3
  videos bana sakta hai. Lambi video ke liye paid server chahiye hoga.
- **Login zaroori hai:** bina Google login ke koi video nahi bana
  sakta aur na hi kisi ki video dekh/download kar sakta hai. Har user
  ki videos sirf usi ko dikhti hain.
- **Security:** hum passwords ya card/UPI details kabhi store nahi
  karte — login Google karta hai, payment Razorpay karta hai. Phir bhi
  koi bhi website 100% hack-proof nahi hoti.
- **Screen recording roki nahi ja sakti:** koi bhi website screen
  recording poori tarah nahi rok sakti. Isi liye preview sirf 30
  second ka aur watermarked hai — poori video ₹1 ke baad milti hai.
- **Preview download nahi hota:** preview sirf dekhne ke liye hai —
  iska link 10 minute mein expire ho jata hai, player mein download
  button nahi hai aur right-click band hai.
- **Ek time par ek video:** chhote free server par render ek-ek karke
  hota hai, pehle aao pehle paao. Queue mein hone par aapko apna
  number dikhega ("aapke aage N video hain"). Server restart ya sleep
  ho jaye to adhoori video apne aap phir se queue mein aa jati hai —
  kisi ki video kho nahi jati.
- **AdSense:** domain lagne ke baad, website par kuch content pages
  (About, Privacy Policy — pehle se hain) ke saath AdSense ke liye
  apply kar sakte ho. Approval mein hafte lagte hain, pehla payment
  $100 par.

## Agar Muse se deploy karwana ho

Hugging Face account bana kar, Settings → Access Tokens mein "Write"
wala token banao aur Muse ko secure tareeke se de do — Muse khud Space
bana kar files upload kar dega. Token kabhi chat mein mat likhna.
