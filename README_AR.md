# محوّل قاعدة بيانات الأدوية المصرية إلى 96 حقلًا — الإصدار V2

هذا المشروع يقرأ قاعدة بيانات الأدوية المصرية العامة:
https://github.com/mahmoudfalous/eg-drugs/blob/main/data/eg_drugs.json

ويحوّلها إلى **96 حقلًا** وفق مخطط `schema_96.json`، مع الحفاظ على السجلات الأصلية في ملف منفصل، وإنتاج تقارير مراجعة.

**تحذير مهم:** المخرجات مسودات تقنية وليست بيانات طبية معتمدة. جميع السجلات الناتجة `is_active=0`. لا يجوز الاعتماد على قيم الصفر في حقول الوصفة أو المخاطر أو التفاعلات الدوائية لإثبات عدم وجود خطر. أي بيانات طبية يجب أن يراجعها صيدلي مؤهل قبل استخدامها.

## إخراج JSON فقط — كل ملف لا يتجاوز 1000 دواء

لتحميل **ملفات JSON فقط دون البيانات الأصلية أو CSV أو ملفات المراجعة**، افتح:

https://github.com/abdallah1mohmmed2/pharmacy-drugs.-/actions/workflows/export_json_batches.yml

- اختر `Export JSON batches - up to 1000 drugs each`، ثم `Run workflow`.
- اختر `batch_size = 1000` (أو 500 لملفات أصغر).
- سيُنشئ البرنامج قاعدة الـ96 حقلًا من نفس `convert.py`، ثم يقسم الناتج إلى أجزاء متتالية دون تكرار أو فرز أو تعديل أي سجل.
- لكل جزء Artifact مستقل باسم `drugs-96-json-batch-01`، `...-02`، إلخ. **داخل كل ملف ZIP يوجد JSON واحد فقط** باسم `batch_01.json`، إلخ.
- على إصدار المصدر الذي يحتوي **27,617** دواءً ومع حجم 1000 سيكون هناك 28 ملفًا: 27 ملفًا × 1000 دواء، وملف أخير يحتوي على 617 دواء.
- يرفض البرنامج أي تكرار في `id` أو `uuid`، أو اختلاف في حقول المخطط الـ96، ويتحقق أن جمع الأجزاء يعيد نفس سجلات JSON الأصلية بنفس الترتيب. لا يحاول دمج سجلين يبدوان متشابهين ولكن بمعرّفين مختلفين؛ فهما قد يكونان مستحضرين مختلفين.
- **لا يرفع سير العمل هذا** `eg_drugs_original.json` أو `original_records.jsonl` أو `mapping_audit.csv` أو `manifest.json` إلى Artifacts. يتم استخدام هذه الملفات مؤقتًا للتحقق داخل GitHub Actions فقط.
- النسخ الناتجة **مسودة غير معتمدة طبيًا**، وجميعها تحتوي نفس الحقول الصيدلانية الـ96 دون إضافة بيانات سريرية لم تتم مراجعتها.

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

## ما الجديد في الإصدار 2.1؟

المشكلة السابقة أن الحقول المنقولة من ملف المصدر كانت محدودة. الآن يقرأ `product_name_parser.py` اسم المنتج باللغة الإنجليزية ويستخرج **عند وضوح الصيغة فقط**:

- `dosage_form` مثل TABLET وSYRUP وCREAM.
- `route` مثل ORAL وTOPICAL وOPHTHALMIC عندما يكون دليلها واضحًا.
- `strength_text` و`strength_value` و`strength_unit` مثل `10mg` أو `0.5%`.

لا يتم استنتاج جرعات الاستخدام للمرضى، ولا تُحوّل التركيبات متعددة المواد أو `10mg/2ml` أو `10/25mg` إلى تركيز عددي واحد. الحقول المستخرجة من الاسم **مبدئية** وتظهر أدلتها في `mapping_audit.csv` وفي ملخص `manifest.json`.

**جلب النشرات الطبية:** اختر `clinical_candidates=20` في GitHub Actions لتجربة الاسترجاع. يعيد `clinical_candidates.py` المعلومات بطريقتين فقط:

1. `CANDIDATE_REQUIRES_PHARMACIST_REVIEW`: المنتج الأمريكي المحتمل متطابق في المادة العلمية والاسم التجاري والتركيز وطريقة الإعطاء ضمن شروط البرنامج؛ تبقى المراجعة مطلوبة.
2. `INGREDIENT_REFERENCE_ONLY_NOT_PRODUCT_MATCH`: معلومات من نشرة أمريكية تخص المادة العلمية، ولكن **ليست دليلًا على أنها النشرة الصحيحة للمنتج المصري**. تظهر المعلومات في `reference_fields` ولا تُكتب في `drugs_96_DRAFT.json` بأي حال.

إذا لم يجد المصدر معلومات مؤكدة، لا يخترعها البرنامج. القيم الافتراضية مثل الصفر لا تعني أن الدواء آمن أو بلا تحذيرات. قد تظل أسعار السوق المحلية والشركة كمرجع رقمي وأرقام التسجيل والقيود الوطنية غير معروفة.

**اختبارات التكامل:** `.github/workflows/tests.yml` الآن يجري اختبارات دون إنترنت، وبعدها يفحص التحويل على أول 20 سجلًا من المصدر الحقيقي ويحفظ معاينة وتقارير في Artifacts.

## بناء قاعدة البيانات الكاملة على GitHub

يوجد مسار مستقل **Build FULL eg-drugs draft catalog** داخل تبويب Actions:

https://github.com/abdallah1mohmmed2/pharmacy-drugs.-/actions/workflows/build_full_draft.yml

أول تشغيل موثّق نجح في تحويل **27,617 سجلًا** (حالة المصدر بتاريخ 9 نوفمبر 2026) إلى 96 حقلًا لكل سجل، مع ملفات الأصل وتقارير المصدر في Artifact باسم `full-eg-drugs-96-fields-DRAFT`. لا تعني الـ96 خانة أن البيانات الطبية الـ96 مكتملة أو معتمدة.

تقارير الاسم أظهرت استخراج الشكل الصيدلاني لـ22,605 سجلًا، وطريقة الإعطاء لـ20,027، والتركيز المفرد لـ9,541. هذه حقول مستخرجة من **اسم المنتج** وليست تحققًا من النشرة، ولذلك تحتاج مراجعة قبل التفعيل.

صفحة تشغيل النسخة الكاملة:
https://github.com/abdallah1mohmmed2/pharmacy-drugs.-/actions/runs/42697572875

يوجد أيضًا **Probe official clinical-label references (REVIEW ONLY)** لاختبار جلب نصوص openFDA وRxNorm من عيّنة متنوعة من الأدوية الفعلية، وحفظها خارج البيانات المعتمدة:

https://github.com/abdallah1mohmmed2/pharmacy-drugs.-/actions/workflows/probe_clinical_labels.yml

عند عدم وجود تطابق مؤكد للتركيز والشكل والشركة/المنتج، قد يجد البرنامج **مرجعًا للمادة الفعالة**، ولكنه ليس نشرة للمنتج المحلي. يُحفظ ضمن `reference_fields` للمراجعة، لا في قاعدة الـ96 حقلًا. قسم `adverse_reactions` يُحفظ منفصلًا دون تصنيفه تلقائيًا شائعًا أو خطيرًا.

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
