# بوت الموسيقى لديسكورد

مشروع بوت موسيقى يعمل على Discord ويُنشر حالياً على Railway باستخدام Docker.

## المتطلبات
- Python 3.12
- FFmpeg
- Deno (يُجهّز داخل صورة Docker لدعم تحديات YouTube)
- متغير البيئة `DISCORD_TOKEN`

## النشر على Railway
1. اربط مستودع GitHub بخدمة Railway.
2. تأكد أن الخدمة تستخدم Dockerfile الموجود في جذر المشروع.
3. أضف `DISCORD_TOKEN` في متغيرات البيئة.
4. أضف `YOUTUBE_COOKIES` فقط إذا كنت تستخدم كوكيز YouTube؛ يجب أن تكون بصيغة Netscape cookies.txt. لا ترفعها إلى GitHub ولا تشاركها.
5. راقب Deploy Logs للتأكد من نجاح البناء وتشغيل البوت.

## ملفات المشروع الأساسية
- `main.py`: أوامر الموسيقى والتشغيل ولوحة التحكم.
- `temp_voice.py`: نظام الرومات الصوتية المؤقتة.
- `patch_main.py`: تعديلات بناء مطلوبة حالياً ويشغّلها Dockerfile.
- `Dockerfile`: تجهيز FFmpeg وDeno واعتماديات Python وتشغيل البوت.
- `requirements.txt`: مكتبات Python.

## ملاحظات
- لا ترفع توكن Discord أو كوكيز YouTube أو أي أسرار إلى GitHub.
- روابط YouTube المباشرة قد تتأثر بقيود YouTube؛ عند فشلها قد يحاول البوت البحث عن نسخة في SoundCloud.
- اكتب `ش س اسم الأغنية` للبحث في SoundCloud فقط.
- يجب تفعيل Message Content Intent من Discord Developer Portal إذا كان البوت يعتمد على قراءة أوامر الرسائل.
