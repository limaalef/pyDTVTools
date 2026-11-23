"""Simula um dtv_tools.py antigo (sem --json): argparse recusa a opção e nada vai para o stdout."""
import sys
sys.stderr.write("dtv_tools.py doctor: error: unrecognized arguments: --json --control\n")
sys.exit(2)
