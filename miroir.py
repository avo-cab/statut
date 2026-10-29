# -*- coding: utf-8 -*-
# deploy/statut/miroir/miroir.py -- le MIROIR de la page « Etat du service », hors d'OVH.
#
# POURQUOI (docs/etat_service_design.md § 7). La page d'etat vit chez OVH, sur le
# staging. Une panne generale d'OVH la rend injoignable au moment precis ou elle
# servirait le plus. Le miroir en garde une copie chez GitHub Pages (Tom, 2026-10-29 :
# « OK pour github pages »), rafraichie toutes les cinq minutes par
# .github/workflows/miroir.yml, dans un depot PUBLIC a part (avo-cab/statut) :
# GitHub Pages ne sert un depot prive qu'avec un forfait payant, et la page est
# publique de toute facon.
#
# CE QU'IL FAIT, a chaque passage :
#   1. il lit chez le service la page (page.html, page.css, page.js) et l'etat de
#      chaque site (/etat/<site>.json) ;
#   2. il ecrit dans <sortie>/ une copie servable telle quelle : la page du premier
#      site a la racine, celle des autres dans <site>/ -- chacune avec son etat.json
#      a cote et l'attribut data-etat-json qui dit a page.js de le lire ;
#   3. SI LE SERVICE NE REPOND PAS, il reprend la derniere copie publiee (lue sur le
#      miroir lui-meme) et sonde chaque site depuis GitHub : la page dit alors que le
#      logiciel repond, ou qu'il est indisponible, depuis quelle heure.
#
# ⚠ IL NE PORTE AUCUNE PAGE A LUI : il recopie celle du service, avec ses textes et ses
# regles de securite (aucun script en ligne). Une retouche de la page se fait dans
# deploy/statut/, et le miroir la reprend au passage suivant.
#
# ⚠ LA LISTE DES SITES est celle de deploy/statut/sites.json, recopiee ici parce que
# le depot du miroir ne voit pas celui du logiciel. test_etat_service.py verifie
# qu'elles ne divergent pas.
#
# Bibliotheque standard seulement : il tourne sur un runner GitHub nu.
#
# Usage : python3 miroir.py <sortie>     (MIROIR_ADRESSE = l'adresse publique du
#         miroir, pour relire la derniere copie ; STATUT_ADRESSE pour un essai)

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen

SERVICE = os.environ.get("STATUT_ADRESSE", "https://statut.carliavocat.fr").rstrip("/")
MIROIR = os.environ.get("MIROIR_ADRESSE", "").rstrip("/")
# Recopie de sites.json (le premier est celui de la racine) -- voir l'en-tete.
SITES = ["my.carliavocat.fr", "staging.carliavocat.fr"]
FICHIERS = ["page.css", "page.js"]

# Les codes de magasin.py.
OK, ARRET, SANS_MESURE = 0, 2, -1


# ── lire ──────────────────────────────────────────────────────────────────
def lire(adresse, delai=20):
	"""Les octets d'une adresse, ou None : une requete perdue ne leve rien ici."""
	try:
		requete = Request(adresse, headers={"User-Agent": "avocab-miroir-statut"})
		with urlopen(requete, timeout=delai) as reponse:
			return reponse.read() if reponse.status == 200 else None
	except (URLError, OSError, ValueError):
		return None


def lire_json(adresse):
	brut = lire(adresse)
	if brut is None:
		return None
	try:
		return json.loads(brut.decode("utf-8"))
	except ValueError:
		return None


def le_site_repond(site):
	"""Trois essais, comme la sentinelle : une requete perdue n'est pas une panne."""
	for essai in range(3):
		if lire("https://{0}/api/method/ping".format(site), delai=15) is not None:
			return True
		if essai < 2:
			time.sleep(10)
	return False


# ── la page du miroir ─────────────────────────────────────────────────────
def page_du_miroir(html, profondeur):
	"""La page du service, avec des adresses relatives et la source de son etat.

	`profondeur` : 0 pour la racine, 1 pour <site>/. Le miroir peut etre servi sous
	un sous-chemin (avo-cab.github.io/statut/) : aucune adresse ne part de « / »."""
	prefixe = "../" * profondeur
	for nom in FICHIERS:
		html = html.replace('"/{0}"'.format(nom), '"{0}{1}"'.format(prefixe, nom))
	html, n = re.subn(r"<html(\s|>)", r'<html data-etat-json="etat.json"\1', html, count=1)
	if not n:
		raise ValueError("la page du service n'a plus de balise <html>")
	return html


# ── l'etat, quand le service ne repond pas ────────────────────────────────
PARIS = timezone(timedelta(hours=1))   # a l'heure pres, l'ete n'y change rien ici


def heure(t):
	"""« 14h05 », a l'heure de Paris (la meme ecriture que vue.py)."""
	try:
		from zoneinfo import ZoneInfo
		zone = ZoneInfo("Europe/Paris")
	except Exception:  # noqa: BLE001 -- un runner sans base des fuseaux
		zone = PARIS
	d = datetime.fromtimestamp(t, zone)
	return "{0}h{1:02d}".format(d.hour, d.minute)


def squelette(site, maintenant):
	"""Un etat complet sans aucune mesure (vue.etat_public, en vide) : ce que page.js
	attend de chaque cle, pour un site dont le miroir n'a encore jamais rien copie."""
	jour = datetime.fromtimestamp(maintenant, timezone.utc).strftime("%Y-%m-%d")
	return {
		"site": site, "verifie": maintenant, "aujourdhui": jour, "jours": 90,
		"global": {}, "incident": None,
		"services": [{"cle": "logiciel", "nom": "Logiciel", "detail": "", "mention": "",
			"code": SANS_MESURE, "dispo": None, "jours": [[SANS_MESURE, 0]] * 90}],
		"courbes": {"fin": maintenant, "reponse": [None] * 96, "charge": [None] * 96},
		"maintenance": None, "incidents": [], "identite": {},
	}


def etat_sans_le_service(precedent, site, repond, maintenant):
	"""L'etat d'un site quand le service de surveillance est injoignable.

	On garde ce que la derniere copie montrait (les bandes de 90 jours, les incidents
	passes) et l'on dit ce que GitHub mesure lui-meme : le logiciel repond, ou non.
	Les autres services n'ont pas de mesure tant que le service ne revient pas."""
	etat = dict(precedent or squelette(site, maintenant))
	miroir = dict(etat.get("miroir") or {})
	depuis = miroir.get("injoignable_depuis") or maintenant
	etat["miroir"] = {"injoignable_depuis": depuis}
	etat["site"] = site
	etat["verifie"] = maintenant
	etat["maintenance"] = None

	services = []
	for s in etat.get("services") or []:
		s = dict(s)
		if s.get("cle") == "logiciel":
			s["code"] = OK if repond else ARRET
		else:
			s["code"] = SANS_MESURE
		services.append(s)
	etat["services"] = services

	a = heure(depuis)
	if repond:
		etat["global"] = {
			"code": OK,
			"phrase": "Le logiciel fonctionne",
			"court": "",
		}
		texte = ("Notre outil de surveillance ne répond plus depuis {0}. Le logiciel, lui, "
			"fonctionne normalement ; le détail de chaque service reviendra avec l'outil.").format(a)
	else:
		etat["global"] = {
			"code": ARRET,
			"phrase": "Le logiciel est indisponible",
			"court": "Le logiciel est indisponible. Nous nous en occupons.",
		}
		texte = ("Nos serveurs ne répondent plus depuis {0}. Nous en sommes informés et "
			"nous nous employons à rétablir le service au plus vite.").format(a)
	etat["incident"] = {"id": "miroir", "messages": [{"heure": a, "texte": texte}]}
	return etat


# ── construire ────────────────────────────────────────────────────────────
def dossier_du_site(rang, site):
	return "" if rang == 0 else site + "/"


def construire(sortie, maintenant=None):
	"""Ecrit le miroir dans `sortie`. Rend True si le service a repondu."""
	maintenant = int(maintenant or time.time())
	page = lire(SERVICE + "/")
	fichiers = {nom: lire(SERVICE + "/" + nom) for nom in FICHIERS}
	service_vivant = page is not None and all(fichiers.values())

	if not service_vivant:
		# La derniere copie publiee, lue sur le miroir lui-meme.
		if not MIROIR:
			raise SystemExit("Le service ne repond pas et MIROIR_ADRESSE n'est pas donnee.")
		# La page d'origine est gardee telle quelle a la racine (page.html) pour ce cas.
		page = lire(MIROIR + "/page.html")
		fichiers = {nom: lire(MIROIR + "/" + nom) for nom in FICHIERS}
		if page is None or not all(fichiers.values()):
			raise SystemExit("Ni le service ni la derniere copie ne repondent : rien n'est publie.")

	os.makedirs(sortie, exist_ok=True)
	for nom, contenu in fichiers.items():
		with open(os.path.join(sortie, nom), "wb") as f:
			f.write(contenu)
	with open(os.path.join(sortie, "page.html"), "wb") as f:
		f.write(page)
	# GitHub Pages ne doit pas passer la copie par Jekyll.
	open(os.path.join(sortie, ".nojekyll"), "w").close()

	for rang, site in enumerate(SITES):
		dossier = os.path.join(sortie, dossier_du_site(rang, site))
		os.makedirs(dossier, exist_ok=True)
		etat = lire_json("{0}/etat/{1}.json".format(SERVICE, site)) if service_vivant else None
		if etat is None:
			precedent = lire_json("{0}/{1}etat.json".format(MIROIR, dossier_du_site(rang, site))) if MIROIR else None
			etat = etat_sans_le_service(precedent, site, le_site_repond(site), maintenant)
		with open(os.path.join(dossier, "index.html"), "w", encoding="utf-8") as f:
			f.write(page_du_miroir(page.decode("utf-8"), 1 if rang else 0))
		with open(os.path.join(dossier, "etat.json"), "w", encoding="utf-8") as f:
			json.dump(etat, f, ensure_ascii=False)
	return service_vivant


if __name__ == "__main__":
	if len(sys.argv) != 2:
		raise SystemExit("Usage : python3 miroir.py <sortie>")
	vivant = construire(sys.argv[1])
	print("Service de surveillance :", "repond" if vivant else "INJOIGNABLE -- etat mesure depuis GitHub")
