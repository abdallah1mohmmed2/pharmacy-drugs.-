# محوّل قاعدة بيانات الأدوية المصرية إلى 96 حقلًا — الإصدار V2

هذا المشروع يقرأ قاعدة بيانات الأدوية المصرية العامة:
https://github.com/mahmoudfalous/eg-drugs/blob/main/data/eg_drugs.json

ويحوّلها إلى **96 حقلًا** وفق مخطط `schema_96.json`، مع الحفاظ على السجلات الأصلية في ملف منفصل، وإنتاج تقارير مراجعة.

**تحذير مهم:** المخرجات مسودات تقنية وليست بيانات طبية معتمدة. جميع السجلات الناتجة `is_active=0`. لا يجوز الاعتماد على قيم الصفر في حقول الوصفة أو المخاطر أو التفاعلات الدوائية لإثبات عدم وجود خطر. أي بيانات طبية يجب أن يراجعها صيدلي مؤهل قبل استخدامها.

## التشغيل من GitHub Actions

1. افتح تبويب **Actions** بالمستودع.
2. اختر **Safe eg-drugs 96-field conversion**.
3. اضغط **Run workflow**.
4. اختر `records=10` للاختبار الأول، و`clinical_candidates=0`.
5. بعد نجاح المهمة، نزّل ملف النتائج من قسم **Artifacts**.
6. لتحويل كامل القاعدة اختر `records=0`.
7. لجلب اقتراحات نصوص النشرات الأمريكية للمراجعة فقط، اختر `clinical_candidates=20` أو `100`؛ لن تُكتب تلك الاقتراحات مباشرة في القاعدة.

## التشغيل على جهازك

يتطلب Python 3.11 أو أحدث، ولا يحتاج مكتبات خارجية.

```bash
python -m unittest discover -s tests -v
python convert.py --download --sample 10 --output-dir output-test
python convert.py --download --sample 0 --output-dir output-full
python clinical_candidates.py --input output-full/drugs_96_DRAFT.json --count 20 --offset 0 --output-dir clinical-review-0
```

## الملفات الأساسية

- `convert.py`: تنزيل ملف JSON وتحويله، مع فحص صحة المعرّفات والمادة الفعالة وأعداد السجلات وبصمة المصدر.
- `schema_96.json`: الحقول المطلوبة وقيمها الافتراضية.
- `label_api.py`: بحث حذر عن نشرات openFDA ومعرّفات RxNorm.
- `clinical_candidates.py`: حفظ اقتراحات النشرات في تقرير مراجعة منفصل دون تعديل السجلات.
- `tests/test_pipeline.py`: اختبارات محلية دون اتصال بالإنترنت.
- `.github/workflows/convert.yml`: تشغيل التحويل من GitHub Actions.
- `.github/workflows/tests.yml`: اختبار المشروع عند الرفع.

## ملفات النتائج

- `outputs/drugs_96_DRAFT.json`: السجلات المحوّلة، كل سجل به 96 حقلًا.
- `outputs/original_records.jsonl`: كل صف أصلي، بما فيه الشركة والسعر بالجنيه المصري.
- `outputs/mapping_audit.csv`: تقرير الحقول التي نُقلت وبيانات التحقق.
- `outputs/manifest.json`: عدد السجلات والنسخة وبصمات SHA256.
- `clinical_outputs/clinical_label_candidates_REVIEW.jsonl`: مقترحات للمراجعة الصيدلانية لا أكثر.

## مبادئ حماية البيانات

- المصدر `active` هو اسم المواد الفعالة وليس حالة تنشيط السجل.
- أسعار المصدر بالجنيه المصري؛ لا تُعامل كأسعار صيدلية بالعملة المحلية.
- `units` لا يُفترض أنه عدد الأقراص.
- لا نعيّن تلقائيًا `requires_prescription` أو `is_controlled` أو جرعات الأطفال أو الحمل والرضاعة.
- يمنع البرنامج استبدال مجلد نتائج موجود ويستخدم بصمات للملفات.
- إن ظهر ملف `FAILED.txt` أو `preflight_errors.json` فلا تعتمد النتائج.

**ترخيص مصدر البيانات:** CC BY 4.0، Egyptian Drug Database by Mahmoud Mohamed:
https://github.com/mahmoudfalous/eg-drugs

**المرجع البرمجي:** https://open.fda.gov/apis/drug/label/ و https://lhncbc.nlm.nih.gov/RxNav/APIs/

هذا المشروع يسهل توحيد بنية قاعدة البيانات لكنه لا يوفر نظام اعتماد طبي آلي.
