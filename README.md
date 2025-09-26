# Exhaust-Leaderboard
Exhaust Leaderboard is a small program that gathers statistics and trends in player summoner spell habits (exhaust count) and usage in their recent League of Legends games.

## Features  
- **Live match detection**: Give a Riot ID and we auto-resolve to PUUID, pull the current ARAM’s 10 participants
- **Per-player ARAM analysis**: For each participant, we scan the last *n* ARAM matches and compute Exhaust usage: raw count + percentage.
- **In-terminal leaderboard**: Sorted by count (then %), with a clear table, proper alignment for non-English names, and a WINNER: prefix on the top player.
- **Built-in rate-limiter**: Global limiter honoring 20 req/s and 100 req/120s from Riot's Developer API key

## Installation  

### Prerequisites  
- Python 3.10+  

## Requirements  

```
requests
```

### Setup  

1. Download ```exhaust_leaderboard.py```:
- Open the file and click Raw → Save As...
- Or run:
```sh
curl.exe -L -o "exhaust_leaderboard.py" "https://raw.githubusercontent.com/Borping/Exhaust-Leaderboard/main/exhaust_leaderboard.py"
   ```  

2. Install dependencies:  
```sh
pip install -r requirements.txt  
   ``` 
3. Generate and copy your [development API key](https://developer.riotgames.com)

4. Paste your API key into the quotes on line 27:
  ```sh
API_KEY   = "YOUR-API-KEY-HERE"
  ```
5. Type the IGN and tag of any player in a live game (ensure the platform is correct)
  ```sh
RIOT_NAME = "YOUR-IGN-HERE"   # gameName (case/space-sensitive; use exactly as shown in client/Lobby)
RIOT_TAG  = "YOUR-TAG-HERE"   # tagLine (e.g. NA1, EUW, 777, etc.)
  ```
6. Run the application:  
```sh
python summoner_tracker.py
   ```  

### Visuals
- **Example Output: A Perfect Score**

![Output](https://i.imgur.com/BjqSyk8.png)

- **16 Player Output (Arena)**

![Arena](https://i.imgur.com/DTrOkaI.png)
