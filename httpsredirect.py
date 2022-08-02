from flask import Flask, redirect

app = Flask(__name__)

@app.errorhandler(404)
def page_not_found(e):
    return redirect("https://benhong.me", code=302)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80)