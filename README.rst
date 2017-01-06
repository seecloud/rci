RCI
###

Event types
***********

========= =============================
name      description
========= =============================
cr        change request (pull request)
push      push (any branch)
cr-closed merged or abandoned
========= =============================

Environment variables
*********************

Github
------

================ ===========
name             description
================ ===========
GITHUB_REPO      Full name of repository, e.g. ``ronf/asyncssh``
GITHUB_HEAD      Head commit e.g. ``master`` or ``f8a98ff9a``
GITHUB_REMOTE    Optional remote repository (fork) e.g. ``redixin/asyncssh``
GITHUB_PR_ACTION One of ``opened``, ``closed``, ``reopened``, or ``synchronize``.
GITHUB_PR_ID     Pull request id
================ ===========

Sample matrix
*************

.. code-block:: yaml

  - matrix:
      name: foo
      projects:
        - org/project
      cr:
        - deploy-test
      push:
        - deploy-staging
      cr-close:
        - cleanup-test
