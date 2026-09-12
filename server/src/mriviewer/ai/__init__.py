"""LLM-written report text, with the measurements as the only source of numbers.

The model never computes anything. Python measures, grades and formats; the
model turns that into Chinese prose, and a guardrail rejects the result if it
contains a number or a structure the data does not support.
"""
