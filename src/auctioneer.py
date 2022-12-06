import matplotlib.pyplot as plt
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.timeframe import TimeFrame
from alpaca.data.requests import CryptoBarsRequest
import pandas as pd
from datetime import datetime
import tensorflow as tf
import numpy as np
from sklearn.preprocessing import MinMaxScaler
import boto3
import os
from io import BytesIO
from dotenv import load_dotenv

load_dotenv()

S3_BUCKET = 'auctioneer1'

MODELS_PATH = '../data/models/'

### Logging

import logging

def create_logger(name, level = 'INFO'):
    logger = logging.getLogger(name)
    syslog = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s: %(message)s', datefmt='%H:%M:%S')
    syslog.setFormatter(formatter)
    logger.setLevel(level)
    logger.addHandler(syslog)

    return logger

### Classes

class Portfolio:

    def __init__(self, balance, data, holdings = {}):
        self.balance = balance
        self.data = data
        self.holdings = holdings
        self.transaction_log = []

    def value(self, date = None):

        if date is not None:
            prices = self.data.loc[date]
        else:
            prices = self.data.iloc[-1]
        
        aum = 0
        for symbol, amount in self.holdings.items():
            price = prices.loc[symbol]
            aum += price * amount
        
        out = {
            'Cash': self.balance,
            'AUM': aum,
            'Total': self.balance + aum
        }

        return out

    def execute(self, date, order_dict):
        if (order_dict is None) | (order_dict == {}):
            return 
        #just goes in order of order
        for security, order in order_dict.items():
            price = self.data.loc[date]['open']

            if order['action'] == 'buy':

                if order['amount'] == 'max':
                    amount = self.balance // price
                else:
                    amount = order['amount']# // price
                
                if amount > 0:
                    if amount * price > self.balance:
                        amount = self.balance // price
                    self.holdings[security] = self.holdings.get(security, 0) + amount
                    self.balance -= amount * price
                    self.transaction_log += [{'date': date, 'action': 'buy', 'amount': amount, 'value': amount*price}]

            elif order['action'] == 'sell':

                currently_held = self.holdings.get(security, 0)
                if  currently_held == 0:
                    continue

                if order['amount'] == 'max':
                    amount = currently_held
                else:
                    amount = order['amount']
                    if amount > currently_held:
                        amount = currently_held
                
                self.holdings[security] -= amount
                self.balance += amount * price
                self.transaction_log += [{'date': date, 'action': 'sell', 'amount': amount, 'value': amount*price}]

    def transaction_log_summary(self):
        buys = []
        sells = []
        for order in self.transaction_log:
            if order['action'] == 'buy':
                buys += [order['value']]
            if order['action'] == 'sell':
                sells += [order['value']]
        
        # print(f"""
        # {len(buys)+len(sells)} total transactions.
        # {len(buys)} buy orders, totalling {sum(buys)}.
        # {len(sells)} sell orders, totalling {sum(sells)}.
        # """)
        return {'buys': (len(buys), sum(buys)), 'sells': (len(sells), sum(sells))}

    def plot_transactions(self):
        transaction_log = self.transaction_log

        for t in transaction_log:
            if t['action'] == 'buy':
                color = 'green'
            else:
                color='red'
            plt.axvline(t['date'], color=color, alpha = .05)
            
        plt.plot(self.data['close'])
        plt.show()


### Pull Data

def pull_data(security, start_date, end_date):
    client = CryptoHistoricalDataClient()
    request_params = CryptoBarsRequest(
        symbol_or_symbols=[security],
        timeframe=TimeFrame.Day,
        start=start_date,
        end=end_date,
        limit = 10_000
    )

    bars_df = client.get_crypto_bars(request_params).df
    data = bars_df.droplevel('symbol').copy()

    return data

def pull_crypto_prices(symbols, start_date, end_date = None, timeframe = 'minute', client = None, column='close'):

    if client is None:
        client = CryptoHistoricalDataClient()
    start_date = pd.to_datetime(start_date)
    if end_date is None:
        end_date = pd.to_datetime(datetime.now())
    else:
        end_date = pd.to_datetime(end_date)
    
    if timeframe.lower() == 'minute':
        timeframe = TimeFrame.Minute
    elif timeframe.lower() == 'day':
        timeframe = TimeFrame.Day
    elif timeframe.lower() == 'month':
        timeframe = TimeFrame.Month
    else:
        raise ValueError(f"Unrecognized timeframe: {timeframe}")
    
    request_params = CryptoBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=timeframe,
        start=start_date,
        end=end_date,
        # limit = 10_000
    )

    bars_df = client.get_crypto_bars(request_params).df

    prices = bars_df[column]
    if isinstance(symbols, str) | len(symbols) == 1:
        print('dropping symbol index')
        prices = bars_df.droplevel('symbol')
    else:
        prices = pivot_price_data(prices.reset_index())

    #TODO: cannot compare tz-naive and tz-aware timestamps
    # pstart = prices.index.get_level_values('timestamp').min()
    # pend = prices.index.get_level_values('timestamp').max()
    # if (prices.shape[0] < 10_000) | (pstart > start_date) | (pend < end_date):
    #     print(f"""
    #     Requested Start: {start_date}
    #     Returned Start: {pstart}
    #     Requested End: {end_date}
    #     Returned End: {pend}
    #     """)

    return prices

def get_crypto_symbols():
    symbols = ['BTC/USD','ETH/USD','DOGE/USD','SHIB/USD','MATIC/USD','ALGO/USD','AVAX/USD','LINK/USD','SOL/USD']
    symbols = ['BTC/USD', 'ETH/USD', 'SOL/USD']
    return symbols

def pivot_price_data(price_data):
    """Pivots price data from tall to wide format, 
    filters data to only after first timestamp where all symbols have non-NA
    """
    if price_data.empty:
        return price_data

    wide_data = price_data.set_index('timestamp')
    wide_data = wide_data.pivot(columns='symbol', values = 'close')
    # try:
    #     first_valid_index = wide_data.index[wide_data.isna().mean(axis = 1)==0][0]
    # except IndexError as e:
    #     print('WARNING: No record where all symbols have valid data')
    #     raise IndexError(f"")
    # wide_data = wide_data.loc[first_valid_index:]
    return wide_data


### Tensorflow

def window_data(df, history_steps, target_steps, train_test_split = .9):
    feature_range = (-10, 10) #-1,1 led to tiny loss values

    df = df.ffill()
    df = df.bfill()

    def split_series(series, n_past, n_future):
        X, Y = list(), list()

        for window_start in range(len(series)):
            past_end = window_start + n_past
            future_end = past_end + n_future
            if future_end > len(series):
                break
            past = series[window_start:past_end, :]
            future = series[past_end:future_end, :]
            X.append(past)
            Y.append(future)
        return np.array(X), np.array(Y)
    
    df = df.sort_index().copy()
    n_features = df.shape[1]
    scalers = {}
    if train_test_split > 0:
        n_train_samples = int(df.shape[0] * train_test_split)
        train, test = df[:n_train_samples], df[n_train_samples:]
        
        for col in train.columns:
            scaler = MinMaxScaler(feature_range=feature_range)
            norm = scaler.fit_transform(train[col].copy().values.reshape(-1,1))
            norm = np.reshape(norm, len(norm))
            scalers[col] = scaler
            train[col] = norm

        for col in train.columns:
            scaler = scalers[col]
            norm = scaler.transform(test[col].copy().values.reshape(-1,1))
            norm = np.reshape(norm, len(norm))
            test[col] = norm
        
        X_train, Y_train = split_series(train.values, history_steps, target_steps)
        X_test, Y_test = split_series(test.values, history_steps, target_steps)
        return X_train, Y_train, X_test, Y_test, scalers
    else: 
        for col in df.columns:
            scaler = MinMaxScaler(feature_range=feature_range)
            norm = scaler.fit_transform(df[col].copy().values.reshape(-1,1))
            norm = np.reshape(norm, len(norm))
            scalers[col] = scaler
            df[col] = norm
        X_df, Y_df = split_series(df.values, history_steps, target_steps)
        return X_df, Y_df, scalers





def predict_forward(data, model, scalers, history = None):
    #TODO: verify you dont need to scale by history steps
    #and it might be worth it for quick time
    if history is not None:
        inference_data = data[-history:]

    for col in inference_data.columns:
        scaler = scalers[col]
        norm = scaler.transform(inference_data[col].values.reshape(-1,1))
        norm = np.reshape(norm, len(norm))
        inference_data[col] = norm

    inference_data = np.array(inference_data).reshape((1, inference_data.shape[0], -1))

    predictions = model.predict(inference_data).squeeze() 
    
    for i, col in enumerate(data.columns):
        scaler = scalers[col]
        predictions[:,i] = scaler.inverse_transform(predictions[:,i].reshape(-1,1)).reshape(-1)

    return predictions


### Benchmarks

def long_return(data, starting_balance):
    starting_average = data.bfill().iloc[0].mean()
    ending_average = data.ffill().iloc[-1].mean()
    return (ending_average / starting_average) * starting_balance
    # long_portfolio = Portfolio(STARTING_BALANCE, data, {})
    # long_portfolio.execute(data.index[0], {SECURITY: {'action': 'buy', 'amount': 'max'}})
    # return long_portfolio.value()['Total']

def dollar_cost_average(price_data, starting_balance = 100_000):
    #This isnt the only way to do it, but it's the way i wrote it first so :)
    per_day = starting_balance / price_data.shape[0]
    symbol_per_day = per_day / price_data.shape[1]

    amounts = symbol_per_day / price_data
    ending_amounts = amounts.sum()

    ending_values = ending_amounts * price_data.iloc[-1]
    final_value = ending_values.sum()

    return final_value


### S3



def create_s3():
    s3 = boto3.client(
        's3',
        aws_access_key_id=os.getenv('AWS_ACCESS_KEY'),
        aws_secret_access_key=os.getenv('AWS_SECRET_KEY'),
    )

    return s3

def load_s3_csv(s3, path):
    s3_object = s3.get_object(Bucket=S3_BUCKET, Key=path)
    contents = s3_object['Body'].read()
    df = pd.read_csv(BytesIO(contents))
    return df

def save_s3_csv(s3, df, path, save_index = True):
    buffer = BytesIO()
    df.to_csv(buffer, index = save_index)
    s3.put_object(Body=buffer.getvalue(), Bucket=S3_BUCKET, Key=path)
    return True

def get_all_s3_objects(s3, **base_kwargs):
    continuation_token = None
    while True:
        list_kwargs = dict(MaxKeys=1000, **base_kwargs)
        if continuation_token:
            list_kwargs['ContinuationToken'] = continuation_token
        response = s3.list_objects_v2(**list_kwargs)
        yield from response.get('Contents', [])
        if not response.get('IsTruncated'):  # At the end of the list?
            break
        continuation_token = response.get('NextContinuationToken')

def load_picture_paths(s3):
    objects_generator = get_all_s3_objects(s3, Bucket=S3_BUCKET, Prefix='saved_pics/')
    objects = [a for a in objects_generator]
    return [object['Key'].replace(S3_BUCKET+'/', '') for object in objects]

def upload_directory(s3, path, key):
    for root, dirs, files in os.walk(path):
        for file in files:
            s3.upload_file(os.path.join(root,file),S3_BUCKET,key+'/'+file)