#!/usr/bin/env python3
'''
Description: Fetches a list of all the Steam ID's and checks the 
current pricing of each game
'''

import sys
import traceback
import time
import requests
import json
import datetime
import random
from sqlalchemy import create_engine, ForeignKey
from sqlalchemy import Column, Date, DateTime, Integer, String
from sqlalchemy import desc, update
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound
from sqlalchemy.sql import func


#Globals
engine = create_engine('sqlite:///games.db')
Base = declarative_base()
API_URL = "http://store.steampowered.com/api/appdetails/"
LIMIT = 100
SLEEPER = 1

#skip cache offset in seconds
skip_offset = 86400

# Don't query anything that is less than this many seconds old
# Set to zero to disable
last_update_offset = 120

#DB Table descriptions
class Blacklist(Base):
    __tablename__ = 'blacklist'
    id = Column(Integer, primary_key=True)
    def __repr__(self):
        return "<Blacklist(id='{}')>".format(self.id)

class Game(Base):
    __tablename__ = 'games'
    id = Column(Integer, primary_key=True)
    name = Column(String(256), nullable=False)
    last_update = Column(DateTime, nullable=False)

    def __repr__(self):
        return "<Game(id='{}', name='{}', last_update='{}')>".format(self.id, self.name, self.last_update)

class Prices(Base):
    __tablename__ = 'prices'
    id = Column(Integer, primary_key=True)
    timestamp         = Column(DateTime, nullable=False, primary_key=True)
    initial_price     = Column(Integer, nullable=False)
    final_price       = Column(Integer, nullable=False)
    discount_percent  = Column(Integer, nullable=False)

    def __repr__(self):
        return "<Prices(id='{}', timestamp='{}', initial_price='{}', final_price='{}', discount_percent='{}'>" . format(self.id, self.timestamp, self.initial_price, self.final_price, self.discount_percent)


# This table stores things we've seen recently, but skipped because they lacked price infomration
class Skipped(Base):
    __tablename__ = 'skipped'
    id = Column(Integer, primary_key=True, nullable=False)
    timestamp = Column(DateTime, nullable=False)
    def __repr__(self):
        return "<Skipped(id='{}', timestamp='{}')>".format(self.id, self.timestamp)

#------------------------------------------------------------------------------

#Dumps the game database (not including prices)
def dump_game_db(session):
    dump = {}
    for g in session.query(Game).all():
        dump[g.id] = g.name
        #print("{} - {}".format(g.name, g.id))
    output = json.dumps(dump)
    return output


#Builds list of all the possible Steam ID's
# returned as a list of dicts (decoded from JSON)
def build_list():
    URL = "http://api.steampowered.com/ISteamApps/GetAppList/v2"
    try:
        response = requests.get(URL)
    except:
        print("Failed to get a list of games from Steam!")
        return False


    print("Game list received, size {} bytes".format(len(response.text)))
    game_list = response.json()["applist"]["apps"]
    return game_list


# Returns list of game ids (ints) stored in the database.
def games_with_data(session):
    return [ g[0] for g in session.query(Game.id,Game.name).all() ]


#Maps the Steam ID to an actual name
def name_matcher(appid, master_list):
    for game in master_list:
        if int(appid) == game['appid']:
            return game['name']

#Queries the DB "Game" table for a given ID
def query_db(session, game_id):
    try:
        result = session.query(Game).filter_by(id=game_id).one()
        return result

    except MultipleResultsFound as MRF:
        print("Error, multiple entries found for ID: {}".format(game_id))
        return False

    except NoResultFound:
        print("No local results found for ID {}. Updating DB".format(game_id))
        return False


def last_price(session,gid):
    try:
        result = session.query(Prices).filter_by(id=gid).order_by(Prices.timestamp.desc()).first()
        return result

    except NoResultFound:
        print("No price history found for ID {}. Updating DB".format(gid))
        return False


#Builds a list of all the blacklist ID's (Those that have no price)
def build_blacklist(session):
    blacklist = []
    for black in session.query(Blacklist).all():
        blacklist.append(black.id)
    return blacklist


# Drops entries in skipped cache older than some threshold
def drop_old_skipped(session):
    try:

        offset=datetime.datetime.utcnow() - datetime.timedelta(0,skip_offset)

        #delete rows
        print("Deleting skipped hosts older than {}".format(offset))

        rows_deleted = session.query(Skipped).filter(Skipped.timestamp < offset).delete()
        session.commit()
        #session.execute(Skipped.delete().where(Skipped.c.timestamp < offset))
        if rows_deleted:
            print("Deleted {} rows.".format(rows_deleted))
    

    except Exception as e:
        print("Unknown error occured building skipped_list! {}".format(type(e).__name__))
        print(traceback.format_exc())
    
    return


# Return list of IDs in the skip list.
# if an ID is in this DB table for any reason, it is skipped
def build_skipped_list(session):
    try:
        skipped=[]
        for skip in session.query(Skipped).all():
            skipped.append(skip.id)

        if last_update_offset:
            for skip in session.query(Game).filter(Game.last_update >= datetime.datetime.utcnow()-datetime.timedelta(0,last_update_offset)).all():
                skipped.append(skip.id)
        return skipped

    except Exception as e:
        print("Unknown error occured building skipped_list! {}".format(type(e).__name__))
        print(traceback.format_exc())

    return []



#Updates the game DB
def update_db(session, table, game, field, value):
    try:
        session.query(table).filter_by(id=game).update({field: value})
    except Exception as e:
        print("Unknown error occured updating the DB! {}".format(type(e).__name__))
        print(sys.exc_info()[0])


# Split a problematic list in half to try and identify the bad ID for blacklisting.
def list_split(session, applist, master_list):
    """Receives a list, splits in half, resends the list of two lists into fetchdump()"""
    newapplist = [applist[::2], applist[1::2]]
    fetchdump(session, newapplist, master_list)
    return


def dump_blacklist(session,master_list):
    blacklist=build_blacklist(session)

    for game in master_list:
        if game["appid"] in blacklist:
            print("Skipping ID {:>6} : Blacklisted: {}".format(game["appid"], game["name"]))


# Update the skipped list stored in the DB.
def process_skipped(session, skip_list, curtime):

    # First, update everyhing already in the database.
    try:
        query = update(Skipped).where(Skipped.id.in_(skip_list)).values(timestamp=curtime)
        print("Skipped update query {}".format(query))

        session.execute(query)

    except Exception as err:
        print("Error updating existing skip list entries! {}".format(err))

    current_skipped_list = build_skipped_list(session)

    pruned_skip_list = list(set(skip_list) - set(current_skipped_list))

    print("New skipped list (working set): {}".format(skip_list))
    #print("Current skipped list in DB: {}".format(current_skipped_list))
    #print("Pruned skipped list(work-db): {}".format(pruned_skip_list))

    # This may be overkill, since we should have already updated existing
    # entries earlier in the function...
    # however, we do need to add new entries, and that is handled here.
    for skipped_game in list(pruned_skip_list): 
        try: 
            result = session.query(Skipped).filter_by(id=skipped_game).one()
            result.timestamp=curtime  # Magic!

        except NoResultFound:
            skip_obj = Skipped(id=skipped_game, timestamp=curtime)
            session.add(skip_obj)

    try:
        session.commit()

    except Exception as err:
        print("Error updating skip list with new entries {}".format(err))

    return



#Main routine for fetching the current price per game
# Arguments:
#   a DB session object
#   a list of game IDs to query
#   a FULL LIST OF ALL GAMES EVAR  (yuck)
def fetchdump(session, appids, master_list):

    all_game_ids = [ game['appid'] for game in master_list ]

    # "applist" is a list itself, within the larger "appids" list
    for applist in appids:

        params = {
            "appids": ",".join(applist),
            "filters": "price_overview"
        }

        # Build the URL parameters
        params_str = '&'.join([ '='.join([x,params[x]]) for x in params.keys() ] )
        print("Fetching URL {}". format(''.join(list([API_URL,'?',params_str]))))

        curtime = datetime.datetime.utcnow()

        try:
            response = requests.get(API_URL, params=params)
        except Exception as e:
            print("Exception occured: {}".format(type(e).__name__))
            continue

        print("Price data received, size {} bytes".format(len(response.text)))


        try:
            data = response.json()

        except Exception as e:
            print("Error requesting data for the following ids: {} \n continuing after splitting them up and retrying".format(", ".join(applist)))
            print("Error type is [{}]".format(type(e).__name__))

            if len(applist) <= 1:
                print("ID {} : {} has invalid json, updating blacklist".format(game, name_matcher(game,master_list)))
                blacklist_obj = Blacklist(id=game)
                session.add(blacklist_obj)

            else:
                list_split(session, applist, master_list)
            continue

        # prune the skip list
        drop_old_skipped(session)
        skip_list = []

        for game in data:
            if data[game]["success"] is True and data[game]["data"]:
                print("ID {:>6} : Updating prices on {}".format(game, name_matcher(game,master_list)))

                initial_price  = data[game]["data"]["price_overview"]["initial"]
                final_price = data[game]["data"]["price_overview"]["final"]
                discount_percent = data[game]["data"]["price_overview"]["discount_percent"]
                name = name_matcher(game, master_list)

                game_found = query_db(session, game)

                if not game_found:
                    # not found, so add it.
                    game_obj = Game(id=game, name=name, last_update=curtime)
                    session.add(game_obj)

                # If we have a price, compare it to the last one.
                # If different, add a new record, otherwise just update
                # the TS on the existing one.
                last_price_found = last_price(session, game)

                # if the current prices match the last one, just update the TS on the last entry
                if last_price_found and (last_price_found.final_price == final_price) and (last_price_found.initial_price == initial_price):

                    # WTF?  How is this better than raw SQL?
                    #query = update(Prices).where(
                    #            and_(
                    #                Prices.id==game, 
                    #                Prices.timestamp==select(
                    #                    func.max(Prices.c.timestamp)).where(
                    #                        Prices.id==game)),
                    #                Prices.final_price == final_price,
                    #                Prices.initial_price == initial_price
                    #            ).values(timestamp=curtime)

                    # update prices set timestamp='<now>' where id=45710 AND timestamp=(select max(timestamp) from prices where id=45710
                    sql = """UPDATE prices 
                             SET timestamp="{}" 
                             WHERE id={} 
                             AND timestamp=(
                                SELECT MAX(timestamp),count(*) AS C 
                                FROM prices 
                                WHERE id={}
                                GROUP BY id
                                HAVING C>1)""".format(curtime,game,game)

                    #print("SQL={}".format(sql))

                else:
                    price_obj = Prices(id=game, final_price=final_price, initial_price=initial_price, timestamp=curtime, discount_percent=discount_percent)
                    session.add(price_obj)


            # We can "successfully" get nothing when asking about prices. This covers demos, F2P games, etc
            elif data[game]["success"] is True and not data[game]["data"]:
                print("ID {:>6} : F2P or demo: {} (updating blacklist)".format(game, name_matcher(game,master_list)))
                try:
                    session.query(Blacklist).filter_by(id=game).one()

                except NoResultFound:
                    blacklist_obj = Blacklist(id=game)
                    session.add(blacklist_obj)
                    session.commit()

            else:
                #No price data yet, check again at later date
                print("ID {:>6} : Lacks price data upstream (skipping): {}".format(game, name_matcher(game,master_list)))
                skip_list.append(game)
                continue

        try:
            session.commit()
        except IntegrityError as err:
            print("Error updating DB! {}".format(err))


        # If we found things to skip (e.g. no price data), then update the skiplist in the DB
        if len(skip_list) > 0:
            process_skipped(session, skip_list, curtime)


        print_stats(session,master_list)
        print("Sleeping {} seconds until the next batch".format(SLEEPER))

        try:
            time.sleep(SLEEPER)
        except KeyboardInterrupt:
            exit(1)


# Show some stats for each loop iteration 
def print_stats(session,master_list):

    all_game_ids = [ game['appid'] for game in master_list ]
    blacklist = build_blacklist(session)
    skipped = build_skipped_list(session)
    games_w_data = games_with_data(session)
    games_wo_data = list(set(all_game_ids) - set(blacklist) - set(games_w_data))


    print("Games total: {}".format(len(master_list)))
    print("Games blacklisted: {}".format(len(blacklist)))
    print("Games on skiplist: {}".format(len(skipped)))
    print("Total games not checked: {}".format(len(blacklist)+len(skipped)))
    print("Games with data in DB: {}".format(len(games_w_data)))
    print("Games without data (total-DB): {}".format(len(games_wo_data)))
    print("Total games to check: {}".format(len(games_wo_data)+len(games_w_data)-len(skipped)))



#Routine for splitting up the queries into chunks of a certain limit
def chunker(l, n):
    """Yield successive n-sized chunks from list l."""
    for i in range(0, len(l), n):
        yield l[i:i+n]


# Build a list of all games for which we have local data,
# then strip out the black-listed items (demos, etc),
# then strip out the skipped items (non-demos, without prices),
# then return a long, chunked list of unseen games, and then those
# we have seen before
def get_ids_to_check(session, master_list):
    all_game_ids = [ game['appid'] for game in master_list ]

    # Build our current blacklist (list of Blacklist objects),
    # and cached skipped games (those with no prices yet)
    blacklist = build_blacklist(session)
    skipped = build_skipped_list(session)

    # Get list of game IDs for which we already have data
    games_w_data = games_with_data(session)

    # Build list of game IDs that lack data.
    games_wo_data = set(all_game_ids) - set(blacklist) - set(games_w_data)
    if skipped:
        games_wo_data = games_wo_data - set(skipped)
    games_wo_data = list(games_wo_data)


    print_stats(session,master_list)

    # Shuffle, shuffle
    #random.shuffle(games_w_data)
    #random.shuffle(games_wo_data)

    ids_to_check = games_wo_data
    ids_to_check.extend(games_w_data)
    ids_to_check = list(map(str,ids_to_check))

    # Chunk the main master list
    return list(chunker(ids_to_check, LIMIT))




#Main method (starting point)
def main():

    # Setup DB connection
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()


    # Fetch list of dicts objects from Steam (game ID/name pairs)
    master_list = build_list()

    if not master_list:
        print("exiting!")
        exit(1)


    #json_game_db = dump_game_db(session)

    ids_to_check = get_ids_to_check(session, master_list)
    fetchdump(session, ids_to_check, master_list)

if __name__ == "__main__":
    main()
