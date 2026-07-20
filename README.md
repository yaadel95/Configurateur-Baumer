*******************************************************************************************************************************************
*                                                                                                                                         *
*                                                                                                                                         *
*                                                                                                                                         *
*                                                                                                                                         *
*******************************************************************************************************************************************
# Configurateur Baumer

Ce dépôt contient un GUI Python basé sur Tkinter pour configurer un inclinomètre Baumer via le protocole CANopen sur un adaptateur PCAN-USB.

## Objectif

L’application permet de :
- se connecter à un capteur Baumer sur le bus CAN,
- lancer une séquence de configuration automatique,
- vérifier et modifier la valeur de HeartBeat,
- vérifier et modifier la valeur de filtre,
- enregistrer puis recharger les paramètres sur le capteur.

## Fonctionnalités principales

Le GUI exécute la séquence suivante :
1. Lecture de la valeur HeartBeat (objet 0x1017:00).
2. Écriture de la valeur cible si nécessaire.
3. Lecture du filtre (objet 0x2603:00).
4. Écriture du filtre cible si nécessaire.
5. Sauvegarde des paramètres (objet 0x1010:01).
6. Rechargement / vérification des paramètres (objet 0x1011:01).

## Prérequis

Avant de lancer l’application, il faut disposer de :
- Python 3.9 ou plus,
- le package Python `python-can`,
- le driver PEAK PCAN-Basic installé sur Windows,
- un adaptateur PCAN-USB et un capteur Baumer connecté au bus CAN.

## Installation

Depuis la racine du projet, exécuter :

```bash
pip install python-can
```

Ensuite, lancer l’application avec :

```bash
python main_baumer.py
```

## Utilisation

1. Ouvrir l’application.
2. Cliquer sur le bouton « Connecter ».
3. Vérifier que l’état passe à « Connecté ».
4. Cliquer sur « Lancer la configuration ».
5. Suivre la progression et lire les valeurs mises à jour dans l’interface.

## Structure des fichiers

- `main_baumer.py` : interface graphique Tkinter et logique de configuration.
- `canopen_client.py` : client CANopen minimal pour les lectures/écritures SDO.
- `PCANBasic.py` : bindings Python pour la bibliothèque PCAN.
- `test_canopen_timeout.py` : test de la gestion des timeouts SDO.

## Notes importantes

- Le nœud CANopen cible est défini par défaut sur l’ID 1.
- Le bitrate utilisé est de 250 kbit/s.
- Le canal PCAN est défini par défaut sur `PCAN_USBBUS1`.
- En cas de problème de communication, vérifier le câblage CAN, l’alimentation du capteur et la présence du driver PCAN.

## Dépannage rapide

Si l’application échoue à communiquer :
- vérifier que l’adaptateur PCAN est bien reconnu par Windows,
- vérifier que le capteur est alimenté,
- vérifier que l’ID du nœud et le canal CAN correspondent à votre configuration matérielle.
