"""Use cases on top of the database.

Services flush but never commit – the caller (route, worker) owns the transaction.
"""
