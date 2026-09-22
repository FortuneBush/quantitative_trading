"""Project test package.

Keeping the test suite as an explicit package avoids accidentally importing an
unrelated third-party ``tests`` distribution when fixtures are shared across
modules (notably on Python 3.9).
"""
