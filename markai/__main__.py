"""``python -m markai`` runs the same CLI as ``mark``.

The console script only exists on a PATH that has the virtualenv active, and "mark is not
recognized" is what a Windows terminal says when it is not. This route works either way.
"""

from markai.cli import main

main()
