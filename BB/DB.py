import os
import sqlite3
import traceback

class GeneralDB:
    ''' basic sqlite3 db which can be used for many purposes
        this isnt very safe probably but i dont really care'''

    def __init__(self, sessionName):
        self.connection = None
        self.cursor = None

        self.sessionName = sessionName # unique String to each instance of this object (The DB name, not the table name)

    def initCursor(self):
        ''' create and set the cursor'''
        os.makedirs("DBSessions", exist_ok=True)
        self.connection = sqlite3.connect("DBSessions/"+self.sessionName+".db")
        self.cursor = self.connection.cursor()

    def closeCursor(self, save=True):
        ''' close the connection and maybe save the changes'''
        if self.connection is None:
            return
        if save:
            self.connection.commit()
        self.connection.close()
        self.connection = None
        self.cursor = None

    def checkCursor(self):
        ''' shorten the cursor init even more'''
        if self.connection is None:
            self.initCursor()

    def rawExecute(self, statement):
        ''' directly execute a command to the db
        note: does not print anything. this meant for ddl related queries'''
        self.checkCursor()
        try:
            self.cursor.execute(statement)
            print("A statement was run: "+str(statement))
        except:
            traceback.print_exc()
        self.closeCursor()

    def rawExecuteAndPrint(self, statement):
        ''' directly execute a select statement'''
        self.checkCursor()
        g = ""
        try:
            self.cursor.execute(statement)
            g = self.cursor.fetchall()
            print(g)
        except:
            traceback.print_exc()
        self.closeCursor()
        return g

    def getItem(self, table, column, rowID):
        ''' get a specific item from a table given a row ID and a column'''
        self.checkCursor()
        try:
            self.cursor.execute("select ? from {} where id=?".format(table), (column, rowID,))
            output = self.cursor.fetchone()
        except:
            traceback.print_exc()
            output = None
        self.closeCursor()
        return output

    def getRow(self, table, rowID):
        ''' get a row from a table given a row ID
        returns None if row doesnt exist or a tuple if it does'''
        self.checkCursor()
        try:
            self.cursor.execute("select * from {} where id=?".format(table), (rowID,))
            output = self.cursor.fetchone()
        except:
            traceback.print_exc()
            output = None
        self.closeCursor()
        return output

    def getColumn(self, table, column):
        ''' get a column from a table as a list'''
        self.checkCursor()
        try:
            self.cursor.execute("select {} from {}".format(column, table))
            output = self.cursor.fetchall()
        except:
            traceback.print_exc()
            output = None
        self.closeCursor()
        return output

    def getTable(self, table):
        ''' get a whole table'''
        self.checkCursor()
        try:
            self.cursor.execute("select * from {}".format(table))
            output = self.cursor.fetchall()
        except:
            traceback.print_exc()
            output = None
        self.closeCursor()
        return output

    def addRow(self, table, values):
        ''' make a new row in a table with the given list of values'''
        self.checkCursor()
        try:
            self.cursor.execute("insert into {} values ({})".format(table, ",".join(values)))
        except:
            traceback.print_exc()
        self.closeCursor()

    def addRows(self, table, values):
        ''' insert many rows into a table
        it is expected that values is a list of rows matching the columns exactly'''
        self.checkCursor()
        try:
            self.cursor.executemany(f"insert into {table} values ({','.join(['?' for _ in values[0]])})",
            values
            )
        except:
            traceback.print_exc()
        self.closeCursor()

    def emptyTable(self, table):
        ''' delete the contents of a table'''
        self.checkCursor()
        try:
            self.cursor.execute("delete from {}".format(table))
        except:
            traceback.print_exc()
        self.closeCursor()

    def delRow(self, table, rowID):
        ''' delete a row from a table with the given row ID'''
        self.checkCursor()
        try:
            self.cursor.execute("delete from {} where id = ?".format(table), (rowID,))
        except:
            traceback.print_exc()
        self.closeCursor()

    def editItem(self, table, rowID, column, newValue):
        ''' edit a value in a table with a given column and row ID'''
        self.checkCursor()
        try:
            self.cursor.execute("update {} set {} = ? where id = ?".format(table, column), (newValue, rowID,))
        except:
            traceback.print_exc()
        self.closeCursor()

    def replaceRow(self, table, rowID, newRowValues):
        ''' edit a row in a table to replace all of its values with new info
        newRowValues should be a list of new values not including the row ID'''
        self.checkCursor()
        try:
            self.delRow(table, rowID)
            self.addRow(table, ['"'+rowID+'"']+newRowValues)
        except:
            traceback.print_exc()
        self.closeCursor()

    def createTable(self, name, columns, suppress=False):
        ''' make a new table with these column names'''
        self.checkCursor()
        try:
            self.cursor.execute("create table {} ({})".format(name, ",".join(columns)))
        except:
            if not suppress:
                traceback.print_exc()
            else:
                pass
        self.closeCursor()

    def verifyTableExists(self, table):
        ''' check to see if a table exists and return true or false'''
        self.checkCursor()
        try:
            self.cursor.execute("select * from {}".format(table))
            output = True
        except:
            output = False
        self.closeCursor()
        return output

    def verifyTableExistsWithRows(self, table, rowIDs):
        ''' check to see if a table exists with the given row IDs
        return a list of row IDs that are missing'''
        missingRows = []
        if not(self.verifyTableExists(table)):
            return rowIDs
        self.checkCursor()
        for rowID in rowIDs:
            try:
                self.cursor.execute("select * from {} where id = {}".format(table, rowID))
                if len(self.cursor.fetchone()) == 0:
                    missingRows.append(rowID)
            except:
                missingRows.append(rowID)
        self.closeCursor()
        return missingRows
