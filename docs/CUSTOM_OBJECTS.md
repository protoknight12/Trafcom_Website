# Собствени обекти: обучение на модел

Детекторът (`detection.py`) знае хора и превозни средства (общ модел YOLOX-s). За ваши обекти (мотокар, палет, ламарина, детайл) се обучава втори модел със същата архитектура (YOLOX), който се слага до първия - откритията им се събират.

## 1. Набор
В „Хале 3D → Свои обекти“: добавете обектите, снимайте от камерите (или качете снимки), очертайте всички видими екземпляри и натиснете **Износ на набора (zip)**.
Колкото повече различни кадри (светлина, ъгли, разстояния, частично закрити), толкова по-добре: на старт 50 до 100 кадъра на обект. Белязвайте **всички** екземпляри на кадъра - небелязан обект се учи като „фон“.

В zip-а: `images/`, `annotations.json` (COCO) и `custom.json` (имената на класовете по ред).

## 2. Обучение (на машина с видеокарта или в Google Colab)
Моделът е YOLOX (Apache-2.0). Командите са по README на https://github.com/Megvii-BaseDetection/YOLOX (проверете ги за вашата версия):

```bash
git clone https://github.com/Megvii-BaseDetection/YOLOX && cd YOLOX && pip install -r requirements.txt && pip install -v -e .
# разархивирайте набора като datasets/COCO/train2017 (images/) и datasets/COCO/annotations/instances_train2017.json (annotations.json);
# за проверка може да сложите същите файлове и като val2017 / instances_val2017.json
cp exps/default/yolox_s.py exps/custom_s.py     # в него: self.num_classes = <брой обекти>; self.input_size = (640, 640); self.test_size = (640, 640); self.max_epoch = 100
# предварително обучени тежести yolox_s.pth - от същата страница с releases
python tools/train.py -f exps/custom_s.py -d 1 -b 16 --fp16 -o -c yolox_s.pth
python tools/export_onnx.py --output-name custom.onnx -n yolox-s -c YOLOX_outputs/custom_s/best_ckpt.pth     # без --decode_in_inference: суров изход, какъвто очаква detection.py
```
Входът трябва да е 640×640 и изходът `[1, 8400, 5 + брой класове]`.

## 3. Пускане
Сложете `custom.onnx` и `custom.json` (от zip-а) в `models/` (на сървъра `/opt/trafcom/models/`, собственик `www-data`) и рестартирайте приложението. Страницата „Свои обекти“ показва „● Собственият модел е качен“.
Откритията от него излизат в рамките, журнала и 3D картата заедно с останалите. Праг на сигурност: `DETECT_MIN_SCORE` (по подразбиране 0.4).
