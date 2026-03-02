# __init__.py dentro satel_fork/satel_integra/
"""
Pacchetto locale fork di satel_integra per la custom integration.
Espone le classi e gli enum principali per import semplici.
"""

from .satel_integra import (
    AlarmState,
    AsyncSatel,
)

from .commands import SatelReadCommand, SatelWriteCommand
from .messages import SatelReadMessage, SatelWriteMessage
# Aggiungi qui altri nomi che servono in giro per il codice
# es. da utils, connection, queue... se importati altrove

__all__ = [
    'AlarmState',
    'AsyncSatel',
    'SatelReadCommand',
    'SatelWriteCommand',
    # ... aggiungi se necessario
]

# Opzionale: log per confermare caricamento
import logging
logging.getLogger(__name__).info(">>> Package satel_integra fork caricato OK - AlarmState esportato")
