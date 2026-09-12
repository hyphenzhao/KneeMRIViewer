"""The chaptered knee report: a fixed template filled from computed data.

`template.py`    reads the YAML skeleton
`radiologist.py` splits the hospital's free-text report into chapters
`sources.py`     one resolver per data source; adding a model = one entry
`document.py`    generate / store / override / review
`render.py`      applies doctor overrides and produces what is displayed and printed
"""
