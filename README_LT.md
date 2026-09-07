# Deye Solar Optimizer v3.1.0

Prognozėmis paremtas, mažai nustatymų įrašų darantis Deye hibridinio inverterio valdiklis per oficialų Deye OpenAPI.

Pagrindinė taisyklė: **skaityti dažnai, rašyti retai**. Telemetrijai naudojamas `/device/latest`, PV prognozei – Open-Meteo, mokymuisi – vietinė SQLite istorija, o eksporto valdymui – apsaugoti `MAX_SELL_POWER` įrašai.

> **Sauga:** pradėkite su `CONTROL_DRY_RUN=true`, patikrinkite teisėtą eksporto ribą ir realias galios ženklų kryptis. Programa niekada neprašo daugiau nei `GRID_EXPORT_HARD_LIMIT_W`.

## Kas naujo v3

- Vienas konfigūracijos failas: `/etc/deye-solar-optimizer/deye.env`.
- Jame yra Deye prisijungimai, stoties/inverterio ID, saulės masyvų kWp/kampai, baterija, eksporto limitas, boilerio ir viryklės grafikai, prognozės bei valdymo saugos parametrai.
- `.env.example` saugu kelti į GitHub; tikro `deye.env` kelti negalima.
- Strategijos: `conservative`, `risky`, `max-export`, `save`, `economic`.
- Boileris ir viryklė modeliuojami kaip suplanuotos apkrovos. Programa jų pati neįjungia.
- Deye `status=500` nepavykę orderiai nebesuvalgo normalaus sėkmingų pakeitimų limito.
- 4 sėkmingi pakeitimai per dieną + galimas 5-as, jei pokytis didelis (pagal nutylėjimą >=500 W). Atskirai ribojama iki 8 teigiamo `orderId` pateikimų per dieną.
- `deye-day-export --hours 6` pateikia tik paskutinių 6 valandų diagnostiką.


## v3.1 analitika ir automatinis mokymasis

v3.1 valdymo kilpą palieka mažą ir konservatyvią, o atskirai prideda **tik skaitantį analitikos sluoksnį**. Dashboard'as Deye valdymo komandų nesiunčia.

Įdiegta:

- istorinis alternatyvių statinių eksporto ribų `0/300/500/800/1000 W` perskaičiavimas;
- interaktyvus bet kokios eksporto ribos slankiklis;
- „oracle“ su tobulu dienos žinojimu ir galutinio SOC sąlyga;
- ekonominis oracle pagal importo/eksporto kainą ir baterijos dėvėjimo kainą;
- faktinės ir prognozuotos kumuliacinės PV energijos grafikas bei prognozės revizijos;
- po pakankamai dienų automatiškai įsijungiantys P10/P20/P50/P80/P90 prognozės kvantiliai;
- po pakankamai v3.1 dienų savaime išmokstama ryto naudingo PV laiko paklaida;
- baterijos throughput, ekvivalentiniai pilni ciklai, laikas >95% ir <20% SOC, C-rate;
- apytikslis PV curtailment įvertinimas;
- MPPT kanalų energijos mokymasis ir pasirinktinė MPPT→PV masyvo sąsaja;
- 30 dienų realaus valdiklio palyginimas su statine eksporto riba ir ekonominis įvertinimas.

Boilerio laiko optimizavimo eksperimento ir bendro appliance scheduler'io v3.1 sąmoningai nėra. Esami boilerio/viryklės parametrai lieka tik planavimo įvestimis.

### Dashboard

```bash
sudo systemctl status deye-solar-analytics
```

Pagal nutylėjimą:

```text
http://127.0.0.1:8787/
```

Saugumo sumetimais klausoma tik localhost. Nuotolinei prieigai geriau naudoti aiškiai sukonfigūruotą reverse proxy.

CLI:

```bash
sudo deyeopt-analytics
sudo deyeopt-analytics --date 2026-09-04 --cap 600
sudo deyeopt-analytics --days 30
```

### Tikimybinė prognozė

```env
FORECAST_PROBABILISTIC_ENABLED=true
FORECAST_PROBABILISTIC_MIN_DAYS=10
FORECAST_PROBABILISTIC_LEARNING_DAYS=45
FORECAST_PROBABILISTIC_QUANTILES="0.10,0.20,0.50,0.80,0.90"
```

Kol nesukaupta bent nustatytas kiekis panašaus prognozės laiko pilnų dienų, tikimybinis modelis **nedalyvauja valdyme**. Vėliau atskirai pagal `day_ahead`, `00-06`, `06-09`, `09-12`, `12-15`, `15-24` mokomasi `faktas/prognozė` santykių ir skaičiuojami P10/P20/P50/P80/P90.

### Ekonominis režimas ir analitika

```env
ECONOMIC_IMPORT_EUR_KWH=0.25
ECONOMIC_EXPORT_EUR_KWH=0.00
ECONOMIC_BATTERY_WEAR_EUR_KWH=0.00
```

Šie parametrai naudojami ekonominiam oracle ir pasirenkamai gyvai `economic` strategijai. Esant statinėms kainoms baterija eksportui naudojama tik tada, kai eksporto pajamos viršija modeliuojamą vėlesnės importuojamos energijos kainą (įvertinus iškrovimo efektyvumą) ir nustatytą baterijos dėvėjimo kainą. Priešingu atveju energija baterijoje saugoma, o eksportuojamas tik prognozės leidžiamas perteklius.

Tai sąmoningai tik **statinių kainų** režimas: v3.1 dar neima Nord Pool ar tiekėjo valandinių tarifų. Kol kainos sąmoningai nesukonfigūruotos, rekomenduojama palikti `conservative`.

## Diegimas

```bash
cp .env.example deye.env
nano deye.env
sudo ./install.sh --env-file ./deye.env
sudo python3 /opt/deye-solar-optimizer/preflight.py
sudo systemctl restart deye-solar-optimizer
sudo deyeopt-status
```

Naujas diegimas pradeda su:

```env
CONTROL_DRY_RUN=true
STRATEGY_ACTIVE="conservative"
```

## Atnaujinimas iš v2.x

```bash
sudo ./upgrade.sh
```

Jei `deye.env` dar nėra, v3 automatiškai konvertuoja seną `config.toml` ir keturis kredencialų failus į vieną `/etc/deye-solar-optimizer/deye.env`. SQLite istorija, appliance grafikai ir buvęs `dry_run` režimas išsaugomi.

Jei fizinį boilerio laikmatį pakeitėte tuo pačiu metu kaip atnaujinimą, galima saugiai perrašyti tik planavimo laiką:

```bash
sudo ./upgrade.sh --water-heater-time 10:00
```

## Pagrindiniai `.env` parametrai

Saulės masyvai:

```env
PV_ARRAYS_JSON='[{"name":"east","kwp":5.0,"tilt_deg":45,"azimuth_deg":-90},{"name":"west","kwp":5.0,"tilt_deg":45,"azimuth_deg":90}]'
```

Azimutai šiame modelyje: `0=S`, `-90=E`, `+90=W`, `+/-180=N`.

Boileris ir viryklė:

```env
WATER_HEATER_ENABLED=true
WATER_HEATER_TIME="10:00"
WATER_HEATER_POWER_W=2000
WATER_HEATER_DURATION_MINUTES=90
WATER_HEATER_ENERGY_KWH=3.0

COOKER_ENABLED=false
COOKER_TIME="18:00"
COOKER_POWER_W=2000
COOKER_DURATION_MINUTES=45
COOKER_ENERGY_KWH=
```

Jei `*_ENERGY_KWH` tuščias, energija apskaičiuojama iš galios ir trukmės. Jei turite realų matavimą, geriau įrašyti realų kWh.

## Strategijos

```bash
sudo deyeopt-strategy status
sudo deyeopt-strategy conservative
sudo deyeopt-strategy risky
sudo deyeopt-strategy max-export
sudo deyeopt-strategy save
sudo deyeopt-strategy economic
```

- `conservative` – rudeniui / silpnai saulei; saugo ryto SOC ir naudoja pesimistišką prognozę.
- `risky` – optimistiškesnis prognozės naudojimas, mažesnis rezervas.
- `max-export` – maksimaliai išnaudoja leidžiamą eksportą ir naktį artėja prie SOC grindų.
- `save` – sąmoningą eksportą uždaro ir saugo energiją vietiniam vartojimui.
- `economic` – pagal `.env` įrašytas statines importo, eksporto ir baterijos dėvėjimo kainas sprendžia, ar apsimoka baterija palaikyti eksportą; jei ne, bateriją saugo ir eksportuoja tik prognozės leidžiamą perteklių.

## Diagnostikos eksportas

Visa šiandiena:

```bash
sudo deye-day-export
```

Paskutinės 6 valandos:

```bash
sudo deye-day-export --hours 6
```

arba trumpai:

```bash
sudo deye-day-export 6
```

Nuo konkretaus laiko:

```bash
sudo deye-day-export --since "2026-09-04 09:00"
```

Pagal nutylėjimą į paketą įdedama tik pasirinkto lango SQLite kopija, žurnalas, JSONL, dabartinis statusas, planas ir redaguotas `.env`. `--full-db` naudokite tik kai reikia ilgalaikės kalibracijos istorijos.

# Mokymosi ir kalibravimo procesas

## 1. Pirmiausia patikrinkite ženklus ir telemetriją

Patikrinkite, kad jūsų firmware:

- teigiamas `BatteryPower` reiškia iškrovimą;
- neigiamas `TotalGridPower` reiškia eksportą;
- `collectionTime` realiai juda pirmyn;
- inverterio ekranas ir Deye duomenys apytiksliai sutampa.

Nekalibruokite iš užstrigusio Deye Cloud cache.

## 2. Inverterio/sistemos nuostoliai

Ramią naktį, kai PV ~= 0:

```text
sistemos nuostoliai ~= baterijos iškrovimas - eksportas - namo apkrova
```

Imkite daugelio švarių taškų medianą ir rašykite į:

```env
LOAD_SYSTEM_OVERHEAD_W=...
```

## 3. Efektyvi baterijos talpa

Per ilgą švarų iškrovimo intervalą:

```text
effective_kWh ~= TotalDischargeEnergy pokytis / (SOC kritimas / 100)
```

Naudokite didelį SOC intervalą ir kelias naktis. Rezultatą rašykite į `BATTERY_EFFECTIVE_KWH`.

## 4. Įkrovimo naudingumas

```text
sukaupta energija ~= effective_kWh * SOC pakilimas/100
charge_efficiency ~= sukaupta energija / išmatuota įkrovimo energija
```

Rezultatą rašykite į `DAY_CHARGE_EFFICIENCY`.

## 5. Įprastas namo vartojimas

Programa renka pilnų dienų `DailyConsumption`. Pirmiausia atimamos žinomos suplanuotos apkrovos (boileris/viryklė), tada mokomasi įprastos namo apkrovos. Iki pakankamai dienų naudojamas `LOAD_FORECAST_FALLBACK_HOUSE_W`.

## 6. Boilerio ir viryklės kalibracija

Nominali galia x laikas tinka pradžiai, bet geriau naudoti realią energiją. Jei termostatas išjungia boilerį anksčiau, `WATER_HEATER_ENERGY_KWH` turi atspindėti realų vidurkį.

## 7. PV geometrija ir `performance_ratio`

Pirmiausia teisingai suveskite kWp, kampą ir azimutą. `PV_PERFORMANCE_RATIO` koreguokite tik pagal kelias reprezentatyvias dienas, o ne vieną debesuotą dieną.

## 8. Prognozės paklaidos mokymasis

DB saugo ankstyviausią pilnos dienos prognozę ir faktinę dienos PV energiją. Programa skaičiuoja:

```text
faktas / prognozė
```

ir, sukaupusi pakankamai dienų, saugiam planavimui ima žemesnį kvantilį. Iki tol naudojamas `FORECAST_DEFAULT_SAFE_FACTOR`.

## 9. Ryto naudingo PV laikas

Naktinis valdymas orientuojasi ne į astronominį saulėtekį, o į pastovią naudingą PV galią:

```env
NIGHT_MORNING_SURPLUS_THRESHOLD_W=350
NIGHT_SUSTAINED_MINUTES=30
NIGHT_FLOOR_LEAD_MINUTES=10
```

Jei kelių dienų faktas nuolat vėluoja/ankstėja dėl horizonto ar šešėlių, koreguokite `PV_WAKEUP_BIAS_MINUTES`.

## 10. Persimokymas keičiantis sezonui

Prasidėjus šildymui, EV įkrovimui ar kitai didelei nuolatinei apkrovai, ankstesnė vartojimo istorija tampa mažiau reprezentatyvi. Pereikite į `conservative` arba `save`, kol susikaups naujo sezono duomenys.

## Deye Cloud užstrigimai

API gali grąžinti `success`, bet tą patį seną duomenų paketą. v3 telemetrijos šviežumui naudoja `collectionTime`; pasiekus `TELEMETRY_CLOUD_OFFLINE_MINUTES`, nauji valdymo įrašai sustabdomi ir paliekama paskutinė saugi vietinė inverterio būsena.

## GitHub privatumas

Į GitHub kelkite kodą, `.env.example` ir dokumentaciją. Nekelkite `deye.env`, SQLite DB, žurnalų, diagnostinių tarball'ų ir senų kredencialų. `.gitignore` tai blokuoja pagal nutylėjimą.
