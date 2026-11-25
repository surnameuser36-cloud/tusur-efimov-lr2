import hashlib
import os
import re
import uuid
from datetime import date, datetime, timezone
from operator import itemgetter

from flask import Flask, make_response, request
from flask_restx import Api, Resource, fields
from werkzeug.middleware.proxy_fix import ProxyFix


VERIFY_TOKEN = "TUSUR-EFIMOV-20261122-42D233D0"
VERIFY_DATE = "2026-11-08"


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

api = Api(
    app,
    version="2.0",
    title="Medical services API",
    description="Веб-сервис врачебных услуг с мягким удалением и верификацией",
    validate=True,
    doc="/",
)

ns = api.namespace("med", description="Врачебные услуги")
disease_ns = api.namespace(
    "disease",
    path="/api/disease",
    description="Эндпоинты для автоматической верификации",
)


med = api.model(
    "MedicalService",
    {
        "id": fields.Integer(readonly=True, description="Уникальный идентификатор услуги"),
        "name": fields.String(required=True, description="Название услуги", example="Лечение зубов"),
        "type": fields.String(required=True, description="Тип услуги", example="Стоматология"),
        "code": fields.String(required=True, description="Код услуги", example="A01.01"),
        "price": fields.Float(required=True, description="Стоимость услуги", example=1000.00),
        "is_deleted": fields.Boolean(readonly=True, description="Признак мягкого удаления"),
        "deleted_at": fields.String(readonly=True, description="Дата и время мягкого удаления"),
    },
)

min_value = api.model(
    "MinValue",
    {"min_value": fields.Float(description="Минимальное значение числового поля")},
)
max_value = api.model(
    "MaxValue",
    {"max_value": fields.Float(description="Максимальное значение числового поля")},
)
avg_value = api.model(
    "AvgValue",
    {"avg_value": fields.Float(description="Среднее значение числового поля")},
)

disease_model = api.model(
    "DiseaseVerification",
    {
        "id": fields.String(readonly=True, description="Идентификатор проверочной записи"),
        "country": fields.String(required=True, description="Страна"),
        "disease": fields.String(required=True, description="Название заболевания или токен"),
        "date": fields.String(required=True, description="Дата"),
        "sick": fields.Integer(required=True, description="Количество заболевших"),
        "recovered": fields.Integer(required=True, description="Количество выздоровевших"),
        "deaths": fields.Integer(required=True, description="Количество умерших"),
    },
)


def normalize_text(text):
    """Приводит текст ответа к каноническому виду для SHA-256."""
    lines = [line.strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    lines = [re.sub(r"\s+", " ", line) for line in lines]
    return "\n".join(lines)


def add_hash_header(response):
    """Добавляет хэш фактически возвращаемого тела в заголовок ответа."""
    body = response.get_data(as_text=True)
    normalized = normalize_text(body)
    response.headers["X-Verify-Hash"] = hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()
    return response


@app.after_request
def add_verify_hash(response):
    """Добавляет X-Verify-Hash к успешным проверочным GET-ответам."""
    disease_path = (
        request.path == "/api/disease/"
        or re.fullmatch(r"/api/disease/[^/]+", request.path) is not None
    )
    if request.method == "GET" and response.status_code == 200:
        if disease_path or request.path == "/verify":
            return add_hash_header(response)
    return response


@app.route("/verify", methods=["GET"])
def verify():
    """Возвращает HTML-страницу с персональным токеном и датой."""
    today = date.today().isoformat()
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="UTF-8"><title>Verification</title></head>
<body>
<h1>Verification</h1>
<p>Token: {VERIFY_TOKEN}</p>
<p>Date: {today}</p>
</body>
</html>"""
    return make_response(html, 200)


@app.route("/health", methods=["GET"])
def health():
    """Возвращает состояние веб-сервиса для проверки Render."""
    return {"status": "ok"}, 200


class Database:
    """Хранит врачебные услуги в памяти приложения."""

    def __init__(self):
        """Создаёт пустое хранилище и счётчик идентификаторов."""
        self.next_id = 1
        self.data = []

    def active(self):
        """Возвращает записи, которые не были мягко удалены."""
        return [item for item in self.data if not item["is_deleted"]]

    def deleted(self):
        """Возвращает мягко удалённые записи."""
        return [item for item in self.data if item["is_deleted"]]

    def get_any(self, service_id):
        """Находит запись по идентификатору независимо от статуса."""
        for item in self.data:
            if item["id"] == service_id:
                return item
        api.abort(404, "Object {} not found".format(service_id))

    def get(self, service_id):
        """Возвращает активную запись по идентификатору."""
        item = self.get_any(service_id)
        if item["is_deleted"]:
            api.abort(404, "Object {} was deleted".format(service_id))
        return item

    def create(self, source):
        """Добавляет новую активную запись."""
        item = dict(source)
        item["id"] = self.next_id
        item["is_deleted"] = False
        item["deleted_at"] = None
        self.next_id += 1
        self.data.append(item)
        return item

    def update(self, service_id, source):
        """Обновляет активную запись, сохраняя служебные поля."""
        item = self.get(service_id)
        values = dict(source)
        for field_name in ("id", "is_deleted", "deleted_at"):
            values.pop(field_name, None)
        item.update(values)
        return item

    def soft_delete(self, service_id):
        """Помечает запись удалённой, не удаляя её из хранилища."""
        item = self.get_any(service_id)
        if item["is_deleted"]:
            api.abort(409, "Object {} is already deleted".format(service_id))
        item["is_deleted"] = True
        item["deleted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return item

    def sort_by(self, field):
        """Сортирует активные записи по разрешённому полю."""
        allowed_fields = {"id", "name", "type", "code", "price"}
        if field not in allowed_fields:
            api.abort(404, "Field '{}' does not exist".format(field))
        return sorted(self.active(), key=itemgetter(field))

    def minimum(self):
        """Возвращает минимальную цену активной услуги."""
        active_items = self.active()
        if not active_items:
            return None
        return min(active_items, key=itemgetter("price"))["price"]

    def maximum(self):
        """Возвращает максимальную цену активной услуги."""
        active_items = self.active()
        if not active_items:
            return None
        return max(active_items, key=itemgetter("price"))["price"]

    def average(self):
        """Возвращает среднюю цену активных услуг."""
        active_items = self.active()
        if not active_items:
            return None
        return sum(item["price"] for item in active_items) / len(active_items)


DB = Database()
DB.create({"name": "Услуга 1", "type": "Тип 1", "code": "A1", "price": 10000.00})
DB.create({"name": "Услуга 2", "type": "Тип 2", "code": "A2", "price": 5000.00})
DB.create({"name": "Услуга 3", "type": "Тип 3", "code": "A3", "price": 1000.00})

DISEASE_DATA = []


@ns.route("/")
class MedicalService(Resource):
    """Возвращает список услуг и создаёт новые услуги."""

    @ns.doc("list_objects", params={"include_deleted": "Показать также удалённые записи"})
    @ns.marshal_list_with(med)
    def get(self):
        """Возвращает активные или все записи."""
        include_deleted = request.args.get("include_deleted", "false").lower() == "true"
        return DB.data if include_deleted else DB.active()

    @ns.doc("create_object")
    @ns.expect(med)
    @ns.marshal_with(med, code=201)
    def post(self):
        """Добавляет новую услугу."""
        return DB.create(api.payload), 201


@ns.route("/deleted")
class MedicalServiceDeleted(Resource):
    """Предоставляет список мягко удалённых услуг."""

    @ns.marshal_list_with(med)
    def get(self):
        """Возвращает записи с установленным признаком is_deleted."""
        return DB.deleted()


@ns.route("/<int:service_id>")
@ns.response(404, "Object not found")
@ns.param("service_id", "Уникальный идентификатор объекта")
class MedicalServiceId(Resource):
    """Возвращает, обновляет и мягко удаляет услугу по ID."""

    @ns.doc("get_object")
    @ns.marshal_with(med)
    def get(self, service_id):
        """Возвращает активную услугу по ID."""
        return DB.get(service_id)

    @ns.doc(
        "soft_delete_object",
        params={"confirm": "Для подтверждения удаления передайте значение true"},
    )
    @ns.response(400, "Deletion is not confirmed")
    @ns.response(409, "Object is already deleted")
    @ns.marshal_with(med)
    def delete(self, service_id):
        """Выполняет подтверждённое мягкое удаление услуги."""
        confirmed = request.args.get("confirm", "false").lower()
        if confirmed not in {"true", "1", "yes"}:
            api.abort(400, "Deletion requires query parameter confirm=true")
        return DB.soft_delete(service_id)

    @ns.expect(med)
    @ns.marshal_with(med)
    def put(self, service_id):
        """Обновляет активную услугу по ID."""
        return DB.update(service_id, api.payload)


@ns.route("/sort/<string:field>")
@ns.response(404, "Invalid field")
@ns.param("field", "Название поля для сортировки")
class MedicalServiceSort(Resource):
    """Возвращает активные услуги в заданном порядке."""

    @ns.marshal_list_with(med)
    def get(self, field):
        """Сортирует активные записи по указанному полю."""
        return DB.sort_by(field)


@ns.route("/min/<string:field>")
@ns.response(404, "Invalid field")
class MedicalServiceMin(Resource):
    """Вычисляет минимальное значение числового поля."""

    @ns.marshal_with(min_value)
    def get(self, field):
        """Возвращает минимальную цену активной услуги."""
        if field != "price":
            api.abort(404, "Field '{}' is not allowed".format(field))
        return {"min_value": DB.minimum()}


@ns.route("/max/<string:field>")
@ns.response(404, "Invalid field")
class MedicalServiceMax(Resource):
    """Вычисляет максимальное значение числового поля."""

    @ns.marshal_with(max_value)
    def get(self, field):
        """Возвращает максимальную цену активной услуги."""
        if field != "price":
            api.abort(404, "Field '{}' is not allowed".format(field))
        return {"max_value": DB.maximum()}


@ns.route("/avg/<string:field>")
@ns.response(404, "Invalid field")
class MedicalServiceAvg(Resource):
    """Вычисляет среднее значение числового поля."""

    @ns.marshal_with(avg_value)
    def get(self, field):
        """Возвращает среднюю цену активных услуг."""
        if field != "price":
            api.abort(404, "Field '{}' is not allowed".format(field))
        return {"avg_value": DB.average()}


@disease_ns.route("/")
class DiseaseVerificationCollection(Resource):
    """Создаёт и возвращает последнюю проверочную запись."""

    @disease_ns.expect(disease_model, validate=True)
    @disease_ns.marshal_with(disease_model, code=201)
    def post(self):
        """Создаёт проверочную запись с новым строковым ID."""
        source = dict(api.payload)
        if source.get("disease") != VERIFY_TOKEN:
            api.abort(400, "Field 'disease' must contain the verification token")
        if source.get("date") != VERIFY_DATE:
            api.abort(400, "Field 'date' must be equal to {}".format(VERIFY_DATE))

        item = {
            "id": uuid.uuid4().hex[:8],
            "country": source["country"],
            "disease": source["disease"],
            "date": source["date"],
            "sick": source["sick"],
            "recovered": source["recovered"],
            "deaths": source["deaths"],
        }
        DISEASE_DATA.append(item)
        return item, 201

    @disease_ns.marshal_with(disease_model)
    def get(self):
        """Возвращает последнюю созданную проверочную запись."""
        if not DISEASE_DATA:
            api.abort(404, "Verification record not found")
        return DISEASE_DATA[-1]


@disease_ns.route("/<string:record_id>")
class DiseaseVerificationItem(Resource):
    """Возвращает проверочную запись по строковому ID."""

    @disease_ns.marshal_with(disease_model)
    def get(self, record_id):
        """Ищет проверочную запись по идентификатору."""
        for item in DISEASE_DATA:
            if item["id"] == record_id:
                return item
        api.abort(404, "Verification record not found")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
