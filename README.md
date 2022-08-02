# CRISiSLab Competition 2021 - Backend

This is the backend of my submission to 2021 CRISiSLab Competition which I won.

This does user management with permissions, earthquake history, live sensor
data, and earthquake alerts with Google Sign In, SQLAlchemy, Socket.io, and
Firebase cloud messaging.

## Running

First, create your env file using the example with your own Firebase API keys,
and Google Client ID and Secret. Then:

```console
$ pip install -r requirements.txt
$ py main.py
```
