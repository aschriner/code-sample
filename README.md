## Andy Schriner code sample

A small sample of code I wrote for LeadGenius back in 2018. I've done plenty more professional software engineering since then but I don't have public samples to share. 

This module was part of a larger application used to parse, combine, and then serve business contact data via an internal API. It consists of a series of Celery tasks that process data files stored on S3 (tens of GB in size), parsing them into individual small jobs, and then executing those individual small jobs to merge Organization-level data as well as person-level data. 

It replaced a complicated, brittle, and long-running spark application with a simpler-to-reason-about, simpler-to-explain, more testable, and vastly faster (runtime reduced from 3 days to 4 hours) processing approach.
