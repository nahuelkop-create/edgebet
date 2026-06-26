import sys
sys.stdout.reconfigure(encoding='utf-8')

from services.anthropic_client import format_match_prompt, generate_picks

match = {
    'homeTeam': {'name': 'Scotland', 'id': 1},
    'awayTeam': {'name': 'Brazil', 'id': 2},
    'competition': {'name': 'Friendly Match', 'id': 999},
    'utcDate': '2026-06-25T18:00:00Z',
    'status': 'SCHEDULED',
    'stage': 'International Friendly',
    'group': 'N/A',
    'matchday': '1'
}

print('--- PROMPT ---')
prompt = format_match_prompt(match)
print(prompt)
print('--- RESULT ---')
try:
    result = generate_picks(match)
    print(result)
except Exception as e:
    print('ERROR:', type(e).__name__, e)
