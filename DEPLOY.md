# Video Studio ko FREE mein online karna (DeployVera)

Ye folder poori website hai. Website GitHub repo
**Jonu2005/video-studio** se DeployVera (free plan) par deploy hoti hai —
Dockerfile se build hoti hai, koi card nahi chahiye. Live URL:
**https://video-studio-a04252.apps.deployvera.com/**

Website ka flow (free launch): **Google se login → roz 3 videos muft
banao → poori Full HD video muft mein download karo.** Har user ki
videos sirf usi ko dikhti hain. Payment abhi band hai (23 Nov 2026 se
5–10 din free launch — customers banane ke liye).

## Part 1 — Deploy kaise hoti hai

1. **Code GitHub par hai:** repo https://github.com/Jonu2005/video-studio
   (branch main). Is folder ki files badlo → GitHub par push karo.
2. **DeployVera service:** panel mein app `video-studio-a04252`
   (service DV-3323FCC6, Container Free — $0/month, no card).
   Source: public GitHub repo, branch main, Dockerfile, port 7860.
3. **Redeploy:** DeployVera panel → deployment page → **Redeploy** dabao.
   Build mein 5–10 minute lagte hain (ffmpeg, voice models ~190MB
   download hote hain). "Live" ho jaye to website khulni chahiye.
4. **Pehle do builds fail hue the (23 Nov 2026):** wajah — Debian 13
   (Trixie) mein espeak-ng ka data folder `/usr/share/espeak-ng-data`
   nahi, `/usr/lib/x86_64-linux-gnu/espeak-ng-data` hota hai, aur
   espeak-ng apne data package ko khud install nahi karta. Dockerfile
   mein ye theek kar diya gaya hai (espeak-ng-data alag se install +
   sahi path) — dobara ye galti mat karna.

## Part 2 — Secrets lagao (bina Google login ke koi video nahi bana sakta)

DeployVera panel → service → **Environment Variables** mein ye daalo
(naam bilkul aise hi likhna hai; `.env.example` mein poori list hai).
Badalne ke baad service restart karni padti hai.

### A. Google login (zaroori)

Pehle se bana hua hai — Google Cloud project **"Video Studio"**
(project ID video-studio-515514), OAuth client "Video Studio Web".
Client ID/secret `~/workspace/video-studio/.secrets/
google-oauth-client.json` mein hai (Muse ke paas, kabhi chat mein
nahi aata).

Env vars:
- `GOOGLE_CLIENT_ID` = OAuth client ID
- `GOOGLE_CLIENT_SECRET` = OAuth client secret
- `OAUTH_REDIRECT_URI` = `https://video-studio-a04252.apps.deployvera.com/auth/callback`
- `SESSION_SECRET` = koi bhi lamba random string (64 hex characters).
  Generate: `python3 -c "import secrets; print(secrets.token_hex(32))"`
  Ise kisi se share mat karna.

**Zaroori:** Google Cloud Console mein OAuth client ke Authorized
redirect URIs mein DeployVera wala URI hona chahiye (upar wala).
Consent screen abhi "Testing" mein hai — sirf test users (owner)
login kar sakte hain. Sabke liye kholne ke liye "Publish App" karna
hoga (homepage + privacy policy URL chahiye — website live hone ke
baad).

### B. Stock footage (recommended — videos mein asli footage ke liye)

- `PIXABAY_API_KEY` = Pixabay API key (pixabay.com par login karke
  API docs page par milti hai). **User khud daalega** (Muse key kabhi
  nahi dekhta). Isse videos mein asli stock footage aati hai. Bina
  key ke bhi video banti hai (Wikimedia Commons + AI images), lekin
  footage kam milti hai.
- `PEXELS_API_KEY` = optional (Pexels ne nayi keys dena abhi rok
  rakha hai — jab dobara mile to daal sakte ho).

### C. Razorpay payment — BAAD MEIN (abhi band hai)

Abhi free launch hai, payment ki zaroorat nahi. Jab payment lagana ho
(free launch ke baad, user ka payment setup hone par):
1. https://razorpay.com par free account banao. **Pehle TEST mode se
   shuru karo** — test keys ke liye KYC nahi chahiye.
2. Dashboard → Settings → API Keys → **Test Mode** on karo → key
   generate karo. `Key ID` aur `Key Secret` milenge.
3. DeployVera env vars mein daalo:
   - `RAZORPAY_KEY_ID` = test Key ID (rzp_test_... se shuru hoti hai)
   - `RAZORPAY_KEY_SECRET` = test Key Secret
4. **Live payment ke liye:** Razorpay mein KYC poora karna hoga (PAN +
   bank account — user khud karega, kuch din lagte hain). KYC ke baad
   live keys (rzp_live_...) milengi — unhe test keys ki jagah daal do.
5. **Code bhi badalna hoga:** payment wala code is samay site mein
   nahi hai. Wapas lagane ke liye Muse se kaho — payment version
   GitHub history mein hai (commit 899d2c38) aur backup mein:
   `~/workspace/video-studio/with-payment-backup/`.
   (Price: ₹1 ya ₹10 — user se confirm karna baaki hai.)

## Part 3 — Check karo

1. Website kholo → **Google se Login** dabao → Google account chuno →
   wapas website par aa jao. Login hona chahiye.
2. Chhota sa script (30–100 shabd) likh kar video banao. Free CPU par
   2-minute video banne mein lagbhag 5–10 minute lag sakte hain —
   normal hai.
3. Video banne ke baad use yahin dekh sakte ho aur **Full HD Video
   download (muft)** ka link dikhna chahiye.

## Support email badalna

Legal pages (About, Privacy, Terms, Refund) mein `[SUPPORT_EMAIL]`
likha hai — ye ek placeholder hai. Apni support email daalne ke liye
`server/static/` ki chaaron HTML files mein `[SUPPORT_EMAIL]` ko apni
email se badal kar GitHub par push karo aur redeploy karo.

## Apna domain lagana (baad mein, jab domain khareed lo)

1. DeployVera panel mein service ke domains section mein apna domain
   add karo (panel exact DNS record batayega).
2. Domain wali website (jahan se khareeda) par DNS record banao jo
   DeployVera ki taraf point kare.
3. **Google OAuth mein bhi naya redirect URI add karna hai:**
   Google Cloud Console → aapka OAuth client → Authorized redirect
   URIs mein `https://<aapka-domain>/auth/callback` add karo, aur
   DeployVera env var `OAUTH_REDIRECT_URI` ko bhi naye domain wale
   URI se badal do.
4. Thodi der mein aapki website apne domain par khulne lagegi.

## Jaankari (honest notes)

- **Free server slow hai:** 0.5 vCPU / 512MB RAM. Render mein time
  lagta hai, aur ek time par ek hi video render hoti hai (FIFO queue —
  aapko apna number dikhega, "aapke aage N video hain"). Server
  restart ho jaye to adhoori video apne aap phir se queue mein aa
  jati hai — kisi ki video kho nahi jati.
- **Video ki limit:** is free setup par ek video zyada se zyada
  lagbhag 400 shabdon ki script (2–3 minute) tak banegi, aur ek user
  roz 3 videos bana sakta hai. Lambi video ke liye paid server
  chahiye hoga.
- **Login zaroori hai:** bina Google login ke koi video nahi bana
  sakta aur na hi kisi ki video dekh/download kar sakta hai. Har user
  ki videos sirf usi ko dikhti hain.
- **Security:** hum passwords ya card/UPI details kabhi store nahi
  karte — login Google karta hai. Phir bhi koi bhi website 100%
  hack-proof nahi hoti.
- **Screen recording roki nahi ja sakti:** koi bhi website screen
  recording poori tarah nahi rok sakti. Abhi free launch hai, isliye
  iska koi matlab nahi — video waise bhi muft download hoti hai.
- **AdSense:** domain lagne ke baad, website par kuch content pages
  (About, Privacy Policy — pehle se hain) ke saath AdSense ke liye
  apply kar sakte ho. Approval mein hafte lagte hain, pehla payment
  $100 par.

## Agar Muse se deploy karwana ho

Muse ke paas GitHub aur DeployVera ka access hai — Muse khud code
push karke redeploy kar deta hai. DeployVera login Secure Vault mein
saved hai; password kabhi chat mein mat likhna.
