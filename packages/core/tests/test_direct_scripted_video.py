from editorial_core.direct_scripted_video import (
    direct_import_report,
    direct_script_draft_document,
    direct_storyboard_templates,
    parse_direct_scripted_video,
)


MASTER_SCRIPT = """
S01 | 00:00–00:54 | Entwicklungszeit als strategische Ressource

Dramaturgische Funktion: Managementrelevanz herstellen.

Voiceover:
Entwicklungszeit ist eine wertvolle Ressource. Jede vermeidbare Stunde für Suchen und Rekonstruieren steht nicht für fachliche Entwicklung zur Verfügung.

Bild/Schnitt: Konzentrierte wissenschaftliche Arbeit, dann Wechsel zwischen Office-Dateien.

On-Screen-Text: Entwicklungszeit ist eine strategische Ressource. / Wie viel davon steht für fachliche Entwicklung zur Verfügung?

Benötigte Assets: Freigegebene F&E-Aufnahmen; bereinigte Office-Screens.

Evidenz-/Freigabehinweis: Keine Einsparungsbehauptung.

S02 | 00:54–01:26 | Ein unverzichtbarer Leistungsnachweis

Dramaturgische Funktion: Single-Site Precision fachlich legitimieren.
Voiceover: Wir betrachten den Single-Site-Precision-Workflow. Er untersucht wiederholte Messungen unter definierten Bedingungen.
Bild/Schnitt: Abgegrenzter Workflow und Punktgruppe für Streuung.
On-Screen-Text: Praxisbeispiel: Single-Site Precision. / Methodische Grundlage: CLSI EP05.
Benötigte Assets: Abstraktes Präzisionsdiagramm; interne SOP-Referenz.
Evidenz-/Freigabehinweis: SOP und CLSI-Ausgabe bestätigen.

S03 | 01:26–01:46 | Komplexität in verlässliche Prozesse überführen
Voiceover: Chromsystems hat technische und regulatorische Komplexität wiederholt in verlässliche Lösungen übersetzt.
Bild/Schnitt: Reduzierte Timeline mit vier Meilensteinen.
On-Screen-Text: Wachstum braucht tragfähige Strukturen.
Benötigte Assets: Freigegebene Timeline-Assets.
Evidenz-/Freigabehinweis: Meilensteine freigeben.

S04 | 01:46–02:12 | Der Entwicklungsprozess ist über Werkzeuge hinausgewachsen
Voiceover: Excel und Word unterstützen viele Einzeltätigkeiten weiterhin sinnvoll. Reibung entsteht an Übergängen zwischen Analyse, Status und Nachweisführung.
Bild/Schnitt: Vorlagenkopie, Datenübernahme und Versionsvergleich.
On-Screen-Text: Office bleibt Werkzeug – aber nicht Prozesssystem.
Benötigte Assets: Bereinigte Legacy-Screen-Captures.
Evidenz-/Freigabehinweis: Nur beobachtete Tätigkeiten zeigen.

S05 | 02:12–02:36 | Die Messmethode
Voiceover: Für den bestehenden Ablauf wurden alle Schritte und Zeitaufwände exakt erfasst. Parallel wird derselbe fachliche Workflow im CS intraNET umgesetzt.
Bild/Schnitt: Methodenkarte mit Fairnesskriterien.
On-Screen-Text: Gleiche Daten. Gleicher Umfang. Gleiche Qualitätsanforderung.
Benötigte Assets: Zeitmessprotokoll; Testdatensatz.
Evidenz-/Freigabehinweis: Bearbeitungszeit und Qualifizierungsaufwand trennen.

S06 | 02:36–03:10 | Der heutige Workflow
Voiceover: Daten werden übernommen, Formeln geprüft, Ergebnisse bewertet und in Word übertragen. Der Aufwand entsteht vor allem an Übergängen.
Bild/Schnitt: Linke Prozesskette mit gemessenen Minuten.
On-Screen-Text: Facharbeit + Medienbrüche + Nachweisorganisation
Benötigte Assets: Legacy-Schrittaufnahmen; freigegebene Prozesszeiten.
Evidenz-/Freigabehinweis: Zeiten exakt übernehmen.

S07 | 03:10–03:44 | Derselbe Workflow im CS intraNET
Voiceover: Im CS intraNET wird derselbe Vorgang als zusammenhängender Datensatz geführt. Änderungen und Entscheidungen bleiben nachvollziehbar.
Bild/Schnitt: Rechte Prozesskette und echte Prototyp-Screens.
On-Screen-Text: Ein Vorgang. Ein Kontext. Durchgängige Nachvollziehbarkeit.
Benötigte Assets: Prototyp-Screen-Captures.
Evidenz-/Freigabehinweis: Ist-Funktionen und Zielbild trennen.

S08 | 03:44–04:24 | Das Messergebnis
Voiceover: Der bestehende Ablauf benötigte [LEGACY_AKTIV_MIN] Minuten aktive Bearbeitungszeit. Im CS intraNET waren es [INTRANET_AKTIV_MIN] Minuten.
Bild/Schnitt: Absolute Zeitwerte und Differenz.
On-Screen-Text: [EINSPARUNG_PROZENT] % weniger aktive Bearbeitungszeit
Benötigte Assets: Freigegebene Benchmarkwerte; Berechnungsnachweis.
Evidenz-/Freigabehinweis: Alle Platzhalter vor Aufnahme ersetzen.

S09 | 04:24–04:56 | Was die Einsparung bedeutet
Voiceover: Diese Differenz bedeutet nicht weniger fachliche Sorgfalt. Sie bedeutet mehr Zeit für Bewertung, Interpretation und Entscheidung.
Bild/Schnitt: Zeitbalken verschiebt sich zu fachlicher Arbeit.
On-Screen-Text: Mehr Zeit für fachliche Arbeit.
Benötigte Assets: Kategorisierte Zeitanteile.
Evidenz-/Freigabehinweis: Kontrolle gleichwertig erhalten.

S10 | 04:56–05:24 | Vom Einzelfall zum belastbaren Potenzial
Voiceover: Ein einzelner Workflow beweist nicht denselben Effekt für die gesamte Forschung und Entwicklung. Weitere Prozesse dürfen erst nach eigener Messung hinzugerechnet werden.
Bild/Schnitt: Ein Vorgang vervielfacht sich bis zur realen Jahreszahl.
On-Screen-Text: Gemessen skalieren – nicht pauschal hochrechnen.
Benötigte Assets: Verifizierte Jahresfrequenz.
Evidenz-/Freigabehinweis: Keine Addition ungemessener Prozesse.

S11 | 05:24–05:52 | Kontrollierter Übergang statt Big Bang
Voiceover: Der nächste Schritt ist ein klar abgegrenzter Pilot für den Single-Site-Precision-Workflow. Erfolg und Betriebsaufwand werden an Kriterien gemessen.
Bild/Schnitt: Pilot-Roadmap mit Scope und KPI-Auswertung.
On-Screen-Text: Begrenzter Pilot. Klare Kennzahlen. Kontrollierte Entscheidung.
Benötigte Assets: Pilotvorschlag; Rollen.
Evidenz-/Freigabehinweis: Validierungsbedarf intern bestätigen.

S12 | 05:52–06:17 | Managemententscheidung und Schluss
Voiceover: Die Entscheidung lautet nicht, ob sofort Excel und Word ersetzt werden. Sie lautet, ob ein gemessener Workflow seine Wirkung belegen darf.
Bild/Schnitt: Beide Pfade enden am gleichen fachlichen Ergebnis.
On-Screen-Text: CS intraNET – Der nächste Meilenstein der F&E.
Benötigte Assets: Freigegebene Wortmarke; Schlusskarte.
Evidenz-/Freigabehinweis: Kein Vollrollout-Versprechen.
"""


def test_direct_scripted_video_parser_extracts_scene_structure() -> None:
    parsed = parse_direct_scripted_video(
        MASTER_SCRIPT,
        title="CS intraNET Single-Site Precision Pilot",
    )

    assert parsed.scene_count == 12
    assert parsed.total_duration_seconds == 377
    assert parsed.scenes[0].scene_key == "s01"
    assert parsed.scenes[0].on_screen_text == (
        "Entwicklungszeit ist eine strategische Ressource",
        "Wie viel davon steht für fachliche Entwicklung zur Verfügung?",
    )
    assert "[LEGACY_AKTIV_MIN]" in parsed.placeholder_tokens
    assert parsed.preview()["scenes"][7]["placeholder_tokens"] == [
        "[EINSPARUNG_PROZENT]",
        "[INTRANET_AKTIV_MIN]",
        "[LEGACY_AKTIV_MIN]",
    ]


def test_direct_scripted_video_builds_no_evidence_script_and_storyboard() -> None:
    parsed = parse_direct_scripted_video(MASTER_SCRIPT, title="Direct production")
    draft = direct_script_draft_document(parsed)
    scenes = direct_storyboard_templates(parsed)
    report = direct_import_report(parsed)

    assert draft["segments"][0]["segment_type"] == "hook"
    assert all(
        annotation["kind"] == "editorial"
        and annotation["claim_ids"] == []
        and annotation["evidence_excerpt_id"] is None
        for segment in draft["segments"]
        for annotation in segment["annotations"]
    )
    assert scenes[0]["segment_key"] == "s01"
    assert scenes[0]["claim_ids"] == []
    assert scenes[0]["source_ids"] == []
    assert scenes[7]["asset_requests"][-1]["kind"] == "placeholder_blocker"
    assert report["evidence_required"] is False
    assert report["requires_independent_verification"] is False
