# بوت الموسيقى لديسكورد

بوت موسيقى وإدارة سيرفرات Discord مكتوب بـPython، ويعمل على Railway باستخدام Docker.

## التشغيل والبناء

يعتمد التشغيل على:
- Python 3.12.
- FFmpeg لمعالجة الصوت.
- Deno لتشغيل تحديات مشغل YouTube عبر yt-dlp.
- BgUtils PO Token Provider، ويُثبّت داخل صورة Docker.
- الحزم المحددة في `requirements.txt`.

للتشغيل على Railway، اربط المستودع بالخدمة واجعل مجلد الجذر هو مجلد المشروع، واترك البناء باستخدام `Dockerfile` الموجود في الجذر. لا تستبدل أمر التشغيل الموجود في Dockerfile إلا إذا تم تحديث إعدادات مزود PO Token معه.

## متغيرات البيئة

**مطلوب**
- `DISCORD_TOKEN`: توكن بوت Discord.

**اختيارية**
- `YOUTUBE_COOKIES`: محتوى ملف cookies بصيغة Netscape عند الحاجة. لا تضعه في GitHub أو README ولا تشاركه.
- `YT_COOKIES_B64`: صيغة Base64 متوافقة مع إعداد الكوكيز القديم.
- `YT_PROXY`: بروكسي اختياري لمصدر YouTube.
- `YTDLP_VERBOSE=1`: تفعيل سجلات yt-dlp التفصيلية للتشخيص.
- `BOT_DATA_DIR`: مجلد حفظ بيانات البوت؛ الافتراضي `/data` إذا كان موجوداً، وإلا مجلد المشروع.
- `VOICE_STATS_FILE`, `STATE_FILE`, `ROLE_OWNERS_FILE`, `NOTIFICATION_CHANNELS_FILE`: لتحديد مسارات ملفات البيانات بشكل منفصل.
- `INTRO_SOUND_FILE`: مسار صوت الدخول؛ الافتراضي `join_sound.mp3`.
- `PORT`: منفذ فحص الخدمة؛ الافتراضي `8080`.

لا تنشئ المتغيرات الاختيارية إلا عند الحاجة. لا تضع قيم الأسرار في ملفات المشروع.

## حفظ البيانات على Railway

إذا أردت استمرار بيانات البوت بعد إعادة النشر، اربط Volume بخدمة Railway على المسار `/data`. لا تحذف الـVolume أو تغيّر مساره أثناء تنظيف ملفات المستودع؛ ملفات البيانات الحية منفصلة عن ملفات الكود.

## النشر على Railway

1. اربط خدمة Railway بمستودع `ix9y3r-hue/my-discord-bot` والفرع `main`.
2. استخدم `Dockerfile` من جذر المستودع.
3. أضف `DISCORD_TOKEN` من صفحة Variables في Railway.
4. أضف `YOUTUBE_COOKIES` فقط إذا كنت تستخدم كوكيز YouTube، وبصيغة Netscape.
5. تأكد من ربط Volume على `/data` إذا كنت تحتاج الاحتفاظ ببيانات الحالة والإحصاءات.
6. بعد كل تحديث، راقب Build Logs ثم Deploy Logs. نجاح البناء لا يثبت وحده أن YouTube يشغّل الصوت؛ يلزم اختبار تشغيل فعلي من Discord.

## بنية الملفات

- `main.py`: أوامر البوت والموسيقى وقائمة التشغيل ولوحة التحكم.
- `temp_voice.py`: نظام الرومات الصوتية المؤقتة.
- `patch_main.py`: تعديلات بناء يطبقها Dockerfile على نسخة `main.py) داخل الصورة.
- `Dockerfile`: تثبيت FFmpeg وDeno ومزود PO Token وتشغيل البوت.
- `requirements.txt`: مكتبات Python.
- `join_sound.mp3`: صوت الدخول التلقائي.
- `.github/workflows/update-notifications.yml`: إرسال إشعار موحد بتحديثات المستودع إلى Discord عند push أو التشغيل اليدوي.

## ملاحظات

- Railway هو هدف النشر المعتمد لهذا المشروع. لا توجد حالياً ملفات Render أو سير عمل Render ضمن شجرة المستودع.
- لا تحذف `Dockerfile` أو `patch_main.py` أو `join_sound.mp3`؛ يعتمد عليها بناء Railway أو ميزة صوت الدخول.
- عند تعذر تشغيل رابط YouTube، قد يحاول البوت البحث عن نسخة في SoundCloud وفق مسار المعالجة الموجود في الكود. قيود YouTube على عناوين IP الخاصة بمراكز البيانات قد تمنع التشغيل حتى مع صحة الإعدادات.
- `ش س` مخصص للبحث في SoundCloud فقط.
- فعّل Message Content Intent من Discord Developer Portal إذا كانت أوامر البوت تعتمد على قراءة محتوى الرسائل.
