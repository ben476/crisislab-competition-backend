import datetime
import itertools
import json
import os
import random
import socket
import string
import threading
import time

import requests
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request, session, g
from flask_socketio import SocketIO, join_room, leave_room
from flask_sqlalchemy import SQLAlchemy
from google.auth.transport import requests
from google.oauth2 import id_token
import functools
import firebase_admin
from firebase_admin import credentials, messaging
from flask_migrate import Migrate

try:
    os.chdir(os.path.dirname(__file__))
except:
    print("in file dir")

# Initialise
load_dotenv()
app = Flask(__name__, static_folder="static", static_url_path="")
app.config["SECRET_KEY"] = os.environ["SECRET_KEY"] or "something secret"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///db.sqlite"
app.config["SQLALCHEMY_COMMIT_ON_TEARDOWN"] = True
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
cred = credentials.Certificate("firebase-creds.json")
firebase_app = firebase_admin.initialize_app(cred)


# google_client_id = os.environ["GOOGLE_CLIENT_ID"]

with open("config.json", encoding="utf-8") as config_file:
    config = json.load(config_file)


def send_quake_alert():
    message = messaging.Message(
        notification=messaging.Notification(
            title=config["notificationTitle"],
            body=config["notificationBody"],
            image=config["notificationImage"],
        ),
        data={"status": "shaking", "timestamp": str(int(time.time() * 1000))},
        topic="quakeAlert",
    )
    messaging.send(message)


# Plugins
db = SQLAlchemy(app)
migrate = Migrate(app, db)
socketio = SocketIO(
    app,
    cors_allowed_origins=[
        "http://localhost:8080",
        "http://localhost:3000",
        "http://localhost:3001",
    ],
)

socketio.init_app(app, cors_allowed_origins="*")

# DB models


class User(db.Model):
    email = db.Column(db.Text, primary_key=True)
    picture = db.Column(db.Text)
    name = db.Column(db.Text)
    given_name = db.Column(db.Text)
    family_name = db.Column(db.Text)
    tokens = db.relationship("Token", backref="user")
    logs = db.relationship("Log", backref="user")
    permissions = db.Column(
        db.JSON, default=lambda: {"users": 2, "quakes": 1, "config": 1, "live": 1}
    )

    def marshal(self):
        return {
            "picture": self.picture,
            "email": self.email,
            "name": self.name,
            "firstName": self.given_name,
            "familyName": self.family_name,
            "permissions": self.permissions,
            "logs": list(reversed([log.marshal() for log in self.logs])),
        }

    def short_marshal(self):
        return {
            "picture": self.picture,
            "email": self.email,
            "name": self.name,
            "firstName": self.given_name,
            "familyName": self.family_name,
            "permissions": self.permissions,
        }

    # password_hash = db.Column(db.String(256))


class Quake(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    time_start = db.Column(db.DateTime, index=True)
    time_end = db.Column(db.DateTime, index=True)
    average = db.Column(db.Float)
    offset = db.Column(db.Float)
    strength = db.Column(db.Float)
    data = db.Column(db.JSON)

    # room = db.relationship("Room", backref=db.backref("messages", lazy=True))

    def marshal(self):
        average_ehz = sum(self.data["EHZ"]) / len(self.data["EHZ"])
        deviance_sum = sum([abs(i - average_ehz) for i in self.data["EHZ"]])
        duration = self.time_end.timestamp() - self.time_start.timestamp()
        intensity = int(deviance_sum / duration)

        return {
            "id": self.id,
            "timeStart": int(self.time_start.timestamp() * 1000),
            "timeEnd": int(self.time_end.timestamp() * 1000),
            "average": int(self.average),
            "offset": int(self.offset),
            "strength": intensity,
            "data": self.data,
        }

    def short_marshal(self):
        average_ehz = sum(self.data["EHZ"]) / len(self.data["EHZ"])
        deviance_sum = sum([abs(i - average_ehz) for i in self.data["EHZ"]])
        duration = self.time_end.timestamp() - self.time_start.timestamp()
        intensity = int(deviance_sum / duration)

        return {
            "id": self.id,
            "timeStart": int(self.time_start.timestamp() * 1000),
            "timeEnd": int(self.time_end.timestamp() * 1000),
            "average": int(self.average),
            "offset": int(self.offset),
            "strength": intensity,
        }


class Log(db.Model):
    timestamp = db.Column(
        db.DateTime, primary_key=True, index=True, default=datetime.datetime.now
    )
    endpoint = db.Column(db.Text)
    method = db.Column(db.String(5))
    parameters = db.Column(db.JSON)
    user_email = db.Column(db.String(320), db.ForeignKey("user.email"))
    # room = db.relationship("Room", backref=db.backref("messages", lazy=True))

    def marshal(self):
        return {
            "timestamp": int(self.timestamp.timestamp() * 1000),
            "endpoint": self.endpoint,
            "method": self.method,
            "parameters": self.parameters,
            "userEmail": self.user_email,
        }


class Token(db.Model):
    token = db.Column(
        db.String(256),
        primary_key=True,
        default=lambda: "".join(
            [random.choice(string.ascii_letters + string.digits) for n in range(64)]
        ),
    )
    expiry = db.Column(
        db.DateTime,
        default=lambda: datetime.datetime.now() + +datetime.timedelta(weeks=1),
    )
    user_email = db.Column(db.Text, db.ForeignKey("user.email"))


class FirebaseSubscription(db.Model):
    token = db.Column(db.String(256), primary_key=True)
    user_agent = db.Column(db.Text)
    user_email = db.Column(db.Text, db.ForeignKey("user.email"))

    def marshal(self):
        return {
            "token": self.token,
            "userAgent": self.user_agent,
            "userEmail": self.user_email,
        }


restart_thread = False

shaking = False


def listen_raw():
    global shaking
    s = None
    bufferSize = 1024
    global restart_thread

    while True:
        try:
            print("thread start")
            restart_thread = False
            if s:
                s.close()
                s = None

            if config["recieveMode"] == "server":
                s = socket.socket(family=socket.AF_INET, type=socket.SOCK_DGRAM)
                s.settimeout(10)
                s.bind(("0.0.0.0", config["serverPort"]))
            else:
                s = socket.socket(family=socket.AF_INET, type=socket.SOCK_DGRAM)
                s.settimeout(10)
                s.sendto(
                    b"connect", (config["proxyServerIP"], config["proxyServerPort"])
                )

            global raw_strengths
            data = {"timestamp": 0, "data": {}}
            time_start = 0
            last_threshold_time = 0
            shaking = False
            quake_strengths = []
            quake_data = {"EHZ": [], "ENN": [], "ENZ": [], "ENE": []}
            while True:
                if restart_thread:
                    break

                msgFromServer = s.recvfrom(bufferSize)
                msg = msgFromServer[0].decode()
                msg = json.loads(
                    msg.replace("'", '"').replace("{", "[").replace("}", "]")
                )

                channel = msg[0]
                timestamp = msg[1]
                msg.pop(0)
                msg.pop(0)
                packets = msg

                if data["timestamp"] != timestamp:
                    # print(data)
                    if data["data"] and all(
                        [i in data["data"] for i in ["EHZ", "ENE", "ENN", "ENZ"]]
                    ):
                        # total_packets = list(
                        #     itertools.chain.from_iterable(data["data"].values())
                        # )
                        total_packets = data["data"]["EHZ"]
                        average_sum = sum([abs(i) for i in total_packets]) / len(
                            total_packets
                        )
                        raw_strengths.append(average_sum)
                        prev_average = sum(raw_strengths) / len(raw_strengths)
                        data["average"] = average_sum - prev_average
                        # print(data)
                        if len(raw_strengths) > 4 * config["detrendTime"]:
                            raw_strengths.pop(0)
                        strength = average_sum - prev_average
                        socketio.emit(
                            "raw data",
                            {
                                **data,
                                "timestamp": data["timestamp"] * 1000,
                                "offset": sum(raw_strengths) / len(raw_strengths),
                                "threshold": config["threshold"],
                                "shaking": shaking,
                            },
                            to="raw data",
                        )
                        # print(
                        #     round(average_sum),
                        #     "   ",
                        #     round(sum(raw_strengths) / len(raw_strengths)),
                        #     "   ",
                        #     round(strength),
                        # )
                        # print(strength, config["threshold"])
                        if strength > config["threshold"]:
                            last_threshold_time = timestamp
                            if not shaking:
                                shaking = True
                                print("shaking start")
                                time_start = data["timestamp"]
                                socketio.emit("shaking start")
                                threading.Thread(
                                    target=send_quake_alert,
                                    daemon=True,
                                ).start()
                        elif shaking and timestamp - last_threshold_time > 5:
                            shaking = False
                            print("shaking stop")

                            average_ehz = sum(quake_data["EHZ"]) / len(
                                quake_data["EHZ"]
                            )
                            deviance_sum = sum(
                                [abs(i - average_ehz) for i in quake_data["EHZ"]]
                            )
                            duration = data["timestamp"] - time_start
                            intensity = deviance_sum / duration

                            quake = Quake(
                                offset=sum(raw_strengths) / len(raw_strengths),
                                average=sum(quake_strengths) / len(quake_strengths),
                                strength=intensity,
                                time_start=datetime.datetime.fromtimestamp(time_start),
                                time_end=datetime.datetime.fromtimestamp(
                                    data["timestamp"]
                                ),
                                data=quake_data,
                            )

                            db.session.add(quake)

                            db.session.commit()

                            socketio.emit(
                                "shaking stop",
                                {
                                    "timeStart": time_start,
                                    "timeEnd": data["timestamp"],
                                    "strength": intensity,
                                    "offset": sum(raw_strengths) / len(raw_strengths),
                                    "average": sum(quake_strengths)
                                    / len(quake_strengths),
                                    "data": quake_data,
                                    "id": quake.id,
                                },
                            )
                        if shaking:
                            quake_strengths.append(average_sum)
                            for i in data["data"]:
                                quake_data[i] += data["data"][i]
                        else:
                            quake_strengths = []
                            quake_data = {"EHZ": [], "ENN": [], "ENZ": [], "ENE": []}

                    data = {"timestamp": timestamp, "data": {}}

                data["data"][channel] = packets
        except:
            pass


data_thread = threading.Thread(target=listen_raw, daemon=True)

# Routes

# Permissions:
#   0: write
#   1: read
#   2: none
def access_level(**levels):
    def view_handler(view):
        @functools.wraps(view)
        def wrapped_view(*args, **kwargs):
            token = Token.query.get(request.headers.get("Authorization"))
            if token is None:
                abort(401)
            if token.expiry < datetime.datetime.now():
                abort(401)

            user = token.user

            for i in levels:
                if levels[i] < user.permissions[i]:
                    abort(403)

            log = Log(
                endpoint=request.path,
                method=request.method,
                parameters=request.values.to_dict(),
                user=user,
            )

            db.session.add(log)
            db.session.commit()

            return view(*args, **kwargs)

        return wrapped_view

    return view_handler


@app.route("/api/v1/alert", methods=["GET"])
@access_level(live=0)
def test_quake_alert():
    send_quake_alert()
    return {}


# Called after the client has recieved the spotify code
@app.route("/api/v1/google-callback", methods=["POST"])
def google_callback():
    token = request.values.get("token")
    if token is None:
        abort(400)
    print("token", token)
    # Get users info from Google with the provided token
    idinfo = id_token.verify_oauth2_token(
        token, requests.Request(), config["googleClientId"]
    )
    if idinfo["iss"] not in ["accounts.google.com", "https://accounts.google.com"]:
        raise ValueError("Wrong issuer.")

    print(idinfo)

    if idinfo["azp"] != config["googleClientId"]:
        abort(403)

    user = User.query.get(idinfo["email"])
    if user is None:
        abort(401)

    user.name = idinfo["name"]
    user.given_name = idinfo["given_name"]
    user.family_name = idinfo["family_name"]
    user.picture = idinfo["picture"]

    # Create token
    token = Token(
        user=user,
    )
    db.session.add(token)

    db.session.commit()
    return {
        **user.short_marshal(),
        "token": token.token,
        "expiry": token.expiry.timestamp() * 1000,
    }


@app.route("/api/v1/users/me", methods=["GET"])
@access_level(users=3)
def get_me():
    token = Token.query.get(request.headers.get("Authorization"))
    if token is None or token.expiry < datetime.datetime.now():
        abort(401)

    user = token.user
    return user.short_marshal()


@app.route("/api/v1/users/<id>", methods=["GET"])
@access_level(users=1)
def get_user(id):
    user = User.query.get(id)
    if not user:
        abort(404)
    return user.marshal()


@app.route("/api/v1/users/<id>", methods=["DELETE"])
@access_level(users=0)
def delete_user(id):
    user = User.query.get(id)
    if not user:
        abort(404)

    db.session.delete(user)
    db.session.commit()
    return {}


@app.route("/api/v1/users/<id>/permissions", methods=["PUT"])
@access_level(users=0)
def update_user_permissions(id):
    permissions = request.values.get("permissions")
    if permissions is None:
        abort(400)

    user = User.query.get(id)

    if not user:
        abort(404)

    user.permissions = json.loads(permissions)

    db.session.commit()
    return user.marshal()


@app.route("/api/v1/users", methods=["GET"])
@access_level(users=1)
def get_users():
    return {
        "list": [user.short_marshal() for user in User.query.all()],
        "timestamp": int(time.time() * 1000),
    }


@app.route("/api/v1/users", methods=["POST"])
@access_level(users=0)
def create_user():
    email = request.values.get("email")
    permissions = request.values.get("permissions")
    if email is None:
        abort(400)

    user = User.query.filter_by(email=email).first()

    if user:
        abort(303)

    if permissions:
        user = User(email=email, permissions=json.loads(permissions))
    else:
        user = User(
            email=email,
        )

    db.session.add(user)

    db.session.commit()
    return (
        user.short_marshal(),
        201,
    )


@app.route("/api/v1/config", methods=["POST"])
@access_level(config=0)
def set_config():
    new_config = json.loads(request.values.get("config"))

    global config
    config = {**config, **new_config}

    if "recieveMode" in config:
        global restart_thread
        restart_thread = True

    if not config:
        abort(400)

    with open("config.json", "w") as config_file:
        json.dump(config, config_file)

    return (
        {"config": config, "timestamp": int(time.time() * 1000)},
        201,
    )


@app.route("/api/v1/config", methods=["GET"])
@access_level(config=1)
def get_config():
    return jsonify({"config": config, "timestamp": int(time.time() * 1000)})


@app.route("/api/v1/live", methods=["GET"])
def get_shaking_live():
    return jsonify({"shaking": shaking, "timestamp": int(time.time() * 1000)})


raw_strengths = []


@app.route("/api/v1/offset", methods=["DELETE"])
@access_level(live=0)
def reset_offset():
    global raw_strengths
    raw_strengths = []

    return (
        {},
        201,
    )


@app.route("/api/v1/offset", methods=["GET"])
@access_level(live=1)
def get_offset():
    return {
        "offset": int(sum(raw_strengths) / len(raw_strengths)),
        "length": len(raw_strengths),
    }


@app.route("/api/v1/quakes")
def get_quakes():
    result = Quake.query.order_by(Quake.time_end.desc()).paginate(max_per_page=200)
    return {
        "list": [
            i.short_marshal()
            for i in result.items
            # i.short_marshal() for i in Quake.query.order_by(Quake.time_end.desc()).paginate(page=request.values.get("page") or 1, per_page=request.values.get("limit") or 100, max_per_page=None)
        ],
        "timestamp": int(time.time() * 1000),
        "total": result.total,
        "page": int(request.values.get("page") or 1),
        "perPage": int(request.values.get("per_page") or 20),
        "pageSize": len(result.items),
    }


@app.route("/api/v1/quakes/<int:id>", methods=["GET"])
def get_quake(id):
    quake = Quake.query.get(id)
    if not quake:
        abort(404)
    return quake.marshal()


@app.route("/api/v1/quakes/<int:id>", methods=["DELETE"])
@access_level(quakes=0)
def delete_quake(id):
    quake = Quake.query.get(id)
    if not quake:
        abort(404)
    db.session.delete(quake)
    db.session.commit()
    return {}, 204


@app.route("/api/v1/subscription", methods=["POST"])
def subscribe_to_topic():
    token = request.values.get("token")

    if not token:
        abort(400)

    messaging.subscribe_to_topic([token], "quakeAlert")

    subscription = FirebaseSubscription(
        token=token, user_agent=request.headers.get("User-Agent")
    )

    db.session.add(subscription)

    db.session.commit()

    socketio.emit(
        "new client",
        {"userAgent": request.headers.get("User-Agent"), "token": token},
        to="new clients",
    )

    return {}, 204


@app.route("/api/v1/subscription", methods=["DELETE"])
def unsubscribe_to_topic():
    token = request.values.get("token")

    if not token:
        abort(400)

    messaging.unsubscribe_from_topic([token], "quakeAlert")

    db.session.delete(FirebaseSubscription.query.get(token))

    db.session.commit()

    socketio.emit("remove client", {"token": token}, to="new clients")

    return {}, 204


clients = {}


@app.route("/api/v1/clients", methods=["GET"])
@access_level(users=0)
def get_clients():
    return {
        "socketio": clients,
        "firebase": [i.marshal() for i in FirebaseSubscription.query.all()],
        "timestamp": int(time.time() * 1000),
    }


@app.route("/api/v1/alert/socketio", methods=["POST"])
@access_level(live=0)
def send_socketio_test():
    sid = request.values.get("sid")

    if not sid:
        abort(400)

    socketio.emit("shaking start", to=sid)

    return {}, 204


@app.route("/api/v1/alert/firebase", methods=["POST"])
@access_level(live=0)
def send_firebase_test():
    token = request.values.get("token")

    if not token:
        abort(400)

    message = messaging.Message(
        notification=messaging.Notification(
            title=config["notificationTitle"],
            body=config["notificationBody"],
            image=config["notificationImage"],
        ),
        data={"status": "shaking", "timestamp": str(int(time.time() * 1000))},
        token=token,
    )

    messaging.send(message)

    return {}, 204


@socketio.on("connect")
def test_connect(auth):
    print("connection", request.sid)

    user = {}

    token = Token.query.get(request.headers.get("Authorization"))
    if not (token is None or token.expiry < datetime.datetime.now()):
        user = token.user

    clients[request.sid] = {
        "userAgent": request.headers.get("User-Agent"),
        "user": user and user.short_marshal(),
    }

    socketio.emit(
        "new client",
        {
            "userAgent": request.headers.get("User-Agent"),
            "user": user and user.short_marshal(),
            "sid": request.sid,
        },
        to="new clients",
    )


@socketio.on("disconnect")
def test_disconnect():
    print("Client disconnected")
    del clients[request.sid]
    socketio.emit("remove client", {"sid": request.sid}, to="new clients")


@socketio.on("alert")
@access_level(live=0)
def send_socketio_test(sid):
    if not sid:
        abort(400)

    socketio.emit("shaking start", to=sid)

    return "aaa"


@socketio.on("listen raw")
@access_level(live=1)
def join():
    print("listen raw connection")
    join_room("raw data")


@socketio.on("listen clients")
@access_level(users=0)
def join_client_listen():
    print("listen clients connection")
    join_room("new clients")


@socketio.on("disconnect raw")
def leave():
    print("disconnection")
    leave_room("raw data")


@socketio.on("disconnect clients")
def leave_client_listen():
    print("disconnection")
    leave_room("new clients")


@app.route("/")
def index():
    return app.send_static_file("index.html")


if __name__ == "__main__":
    if not os.path.exists("db.sqlite"):
        db.create_all()
    data_thread.start()
    socketio.run(app, port=80, debug=False)
