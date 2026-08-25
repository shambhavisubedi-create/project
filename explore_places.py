"""
explore_places.py
------------------
Standalone Flask app for the GoGather "Explore Places" section.
Run directly: python explore_places.py  ->  http://127.0.0.1:5000
"""

from flask import Flask, render_template_string, redirect, url_for, flash

app = Flask(__name__)
app.secret_key = "gogather-explore-places-secret"

# ---------------------------------------------------------------------------
# Data — 10 Nepal destinations
# ---------------------------------------------------------------------------

PLACES = [
    {"id": 1, "name": "Pokhara", "region": "Gandaki Province", "icon": "🏔️",
     "tagline": "Lakes, peaks & paragliding",
     "desc": "A lakeside city cradled by the Annapurna range. Paddle across Phewa "
             "Lake at dawn, watch the sunrise turn Machapuchare pink from Sarangkot, "
             "or take a leap of faith paragliding over the valley."},
    {"id": 2, "name": "Chitwan National Park", "region": "Bagmati Province", "icon": "🐘",
     "tagline": "Jungle safaris & one-horned rhinos",
     "desc": "Nepal's first national park, home to Bengal tigers, one-horned rhinos "
             "and gharial crocodiles. Ride through the sal forests on a jeep safari "
             "or drift down the Rapti River on a dugout canoe."},
    {"id": 3, "name": "Everest Base Camp", "region": "Province No. 1", "icon": "🧗",
     "tagline": "The classic Khumbu trek",
     "desc": "Twelve days on foot through Sherpa villages, suspension bridges and "
             "rhododendron forest, ending at 5,364m in the shadow of the world's "
             "highest peak. Namche Bazaar and Tengboche monastery break the climb."},
    {"id": 4, "name": "Bhaktapur", "region": "Bagmati Province", "icon": "🏛️",
     "tagline": "A living medieval city",
     "desc": "Pottery Square still spins clay the old way, and Nyatapola Temple "
             "rises five tiers over a brick-paved Durbar Square. Bhaktapur moves at "
             "the pace it always has, largely untouched by traffic."},
    {"id": 5, "name": "Lumbini", "region": "Lumbini Province", "icon": "🕊️",
     "tagline": "Birthplace of Lord Buddha",
     "desc": "A UNESCO World Heritage pilgrimage site marked by the Maya Devi "
             "Temple and the sacred pond where Buddha is said to have taken his "
             "first bath. Monasteries built by two dozen nations ring the Peace Park."},
    {"id": 6, "name": "Rara Lake", "region": "Karnali Province", "icon": "🏞️",
     "tagline": "Nepal's largest, remotest lake",
     "desc": "A deep blue alpine lake in Nepal's most untouched national park, "
             "ringed by pine forest and reached by a short flight to Talcha followed "
             "by a trek. Rara rewards travelers who want solitude over crowds."},
    {"id": 7, "name": "Mustang", "region": "Gandaki Province", "icon": "🏜️",
     "tagline": "The trans-Himalayan desert kingdom",
     "desc": "A high, wind-carved desert once forbidden to outsiders, dotted with "
             "cliffside cave monasteries and the walled city of Lo Manthang. Mustang "
             "feels closer to the Tibetan plateau than to the rest of Nepal."},
    {"id": 8, "name": "Ilam", "region": "Province No. 1", "icon": "🍃",
     "tagline": "Rolling tea gardens in the east",
     "desc": "Terraced tea estates roll into the eastern hills near Darjeeling's "
             "border, with mist settling over the gardens each morning. Ilam is "
             "Nepal's quiet answer to a hill station, best paired with a cup fresh "
             "off the slope."},
    {"id": 9, "name": "Gorkha", "region": "Gandaki Province", "icon": "🏯",
     "tagline": "The hilltop fortress of a kingdom's birth",
     "desc": "Gorkha Durbar sits on a ridge with Manaslu and the Annapurnas on the "
             "horizon, the ancestral home of the Shah dynasty that unified Nepal. "
             "The climb up stone steps to the palace is a pilgrimage in itself."},
    {"id": 10, "name": "Bandipur", "region": "Gandaki Province", "icon": "🏘️",
     "tagline": "A Newari hill town frozen in time",
     "desc": "A car-free ridge-top bazaar of Newari houses with carved windows, "
             "strung between terraced fields and views of the Himalaya on a clear "
             "morning. Bandipur is Nepal's hill-town charm without the crowds of "
             "Pokhara."},
]


def get_place(place_id):
    return next((p for p in PLACES if p["id"] == place_id), None)


# ---------------------------------------------------------------------------
# Shared CSS (same look as the rest of GoGather)
# ---------------------------------------------------------------------------

BASE_CSS = """
* { box-sizing: border-box; }
body { margin:0; background:#FBF3E8; font-family:-apple-system,"Segoe UI",sans-serif; color:#3D2B2E; }
a { text-decoration:none; color:inherit; }
.nav { display:flex; align-items:center; justify-content:space-between; padding:18px 40px;
       background:#FBF3E8; border-bottom:1px solid #ECD9CC; flex-wrap:wrap; gap:12px; }
.logo { font-family:Georgia,"Times New Roman",serif; font-size:22px; font-weight:700; }
.right { display:flex; align-items:center; gap:14px; font-size:14px; color:#6b5a55; }
.badge { background:#F2C9D3; color:#8A3B52; font-weight:700; font-size:11px; letter-spacing:.5px;
         padding:4px 10px; border-radius:999px; }
.main { padding:34px 40px 60px; max-width:1180px; margin:0 auto; }
.section-head { display:flex; align-items:baseline; justify-content:space-between; margin-bottom:20px; flex-wrap:wrap; gap:6px; }
.section-head h2 { font-family:Georgia,serif; font-size:26px; margin:0; }
.section-head span { color:#8a7a75; font-size:13.5px; }
.grid { display:grid; grid-template-columns:repeat(2,1fr); gap:18px; }
.card { background:#fff; border-radius:16px; overflow:hidden; display:flex; flex-direction:column; }
.card-img { height:120px; background:#FBE1E6; display:flex; align-items:center; justify-content:center; font-size:44px; }
.card-body { padding:18px 20px 20px; flex:1; display:flex; flex-direction:column; }
.card-body h3 { font-family:Georgia,serif; font-size:18px; margin:0 0 2px; }
.tagline { color:#B5577A; font-size:12.5px; font-weight:600; margin-bottom:8px; }
.desc { color:#7a6560; font-size:13.5px; line-height:1.5; margin:0 0 14px; flex:1; }
.region-tag { display:inline-block; font-size:11px; color:#8A3B52; background:#FBE1E6;
              padding:3px 10px; border-radius:999px; margin-bottom:10px; width:fit-content; }
.btn { background:#8A3B52; color:#fff; border:none; border-radius:10px; padding:10px 16px;
       font-size:13.5px; cursor:pointer; font-weight:600; display:inline-block; }
.btn:hover { background:#732F44; }
.btn.outline { background:#fff; color:#8A3B52; border:1.5px solid #E9B7C4; }
.btn.outline:hover { background:#FBE1E6; }
.back-link { display:inline-block; color:#8A3B52; font-size:13.5px; font-weight:600; margin-bottom:14px; }
.flash { background:#3D2B2E; color:#fff; padding:12px 22px; border-radius:999px; font-size:13.5px; margin-bottom:18px; display:inline-block; }
@media (max-width:900px){ .grid{grid-template-columns:1fr;} .nav{padding:16px 20px;} .main{padding:24px 20px 50px;} }
"""

BASE_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<title>GoGather — Explore Places</title>
<style>{{ css|safe }}</style></head><body>
<nav class="nav">
  <div class="logo">GoGather</div>
  <div class="right"><span>Welcome, <b>ria</b>!</span><span class="badge">TRAVELLER</span></div>
</nav>
<div class="main">
  {% with messages = get_flashed_messages() %}
    {% if messages %}{% for m in messages %}<div class="flash">{{ m }}</div>{% endfor %}{% endif %}
  {% endwith %}
  {{ body|safe }}
</div>
</body></html>
"""

LIST_BODY = """
<div class="section-head"><h2>Explore Places</h2><span>{{ places|length }} destinations</span></div>
<div class="grid">
  {% for pl in places %}
    <div class="card">
      <div class="card-img">{{ pl.icon }}</div>
      <div class="card-body">
        <span class="region-tag">{{ pl.region }}</span>
        <h3>{{ pl.name }}</h3>
        <div class="tagline">{{ pl.tagline }}</div>
        <p class="desc">{{ pl.desc[:90] }}...</p>
        <a class="btn outline" href="{{ url_for('place_detail', place_id=pl.id) }}">View Details</a>
      </div>
    </div>
  {% endfor %}
</div>
"""

DETAIL_BODY = """
<a class="back-link" href="{{ url_for('places') }}">&larr; Back to Explore Places</a>
<div class="card" style="max-width:640px;">
  <div class="card-img" style="height:180px; font-size:70px;">{{ place.icon }}</div>
  <div class="card-body">
    <span class="region-tag">{{ place.region }}</span>
    <h3 style="font-size:24px;">{{ place.name }}</h3>
    <div class="tagline">{{ place.tagline }}</div>
    <p class="desc" style="font-size:14.5px;">{{ place.desc }}</p>
  </div>
</div>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
@app.route("/places")
def places():
    body = render_template_string(LIST_BODY, places=PLACES)
    return render_template_string(BASE_HTML, css=BASE_CSS, body=body)


@app.route("/places/<int:place_id>")
def place_detail(place_id):
    place = get_place(place_id)
    if not place:
        flash("That destination doesn't exist.")
        return redirect(url_for("places"))
    body = render_template_string(DETAIL_BODY, place=place)
    return render_template_string(BASE_HTML, css=BASE_CSS, body=body)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
