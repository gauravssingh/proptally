"""Document-based portfolio onboarding.

The pipeline is deliberately split along the architectural boundary that matters:

* :mod:`proptally.onboarding.archive` treats the ZIP as hostile input;
* :mod:`proptally.onboarding.extraction` turns a document into text and fields;
* :mod:`proptally.onboarding.pipeline` reconciles documents into proposals;
* :mod:`proptally.onboarding.provisioning` is the only module that writes into a
  portfolio database, and it only ever sees records the customer accepted.

Nothing here imports the web layer, and no provider is called unless it was
configured explicitly (see :mod:`proptally.onboarding.providers`).
"""
