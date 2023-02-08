import time

import shap
import numpy as np
import dalex as dx
import pandas as pd
import xgboost as xgb

from tqdm import tqdm
from goodpoints import kt, compress
from sklearn import metrics
from sklearn.model_selection import train_test_split
from scipy.stats import wasserstein_distance

RANDOM_STATE = 2137
TEST_SIZE = 0.4


class DataProcessor:
    def __init__(self, df=None, target=None, X=None, y=None, test_size=TEST_SIZE, random_state=RANDOM_STATE, to_drop=[],
                 to_cat=[], to_one_hot=[], dropna=True, train_eq_test=False):
        assert (df is not None and target is not None) or (X is not None and y is not None)

        self.df = df
        self.target = target
        self.test_size = test_size
        self.random_state = random_state
        self.to_drop = to_drop
        self.to_cat = to_cat
        self.to_one_hot = to_one_hot
        self.dropna = dropna
        self.train_eq_test = train_eq_test

        self.clean_df = None
        self.X = X
        self.y = y
        self.X_train = None
        self.X_test = None
        self.y_train = None
        self.y_test = None

        self.__preprocess_data()
        self.__split_data()

    def __preprocess_data(self):
        if self.df is None:
            self.X = pd.DataFrame(self.X)
            self.y = pd.DataFrame(self.y)
            self.X.columns = self.X.columns.astype(str)
            self.y.columns = self.y.columns.astype(str)
            return

        clean_df = self.df

        if self.to_drop:
            clean_df = clean_df.drop(columns=self.to_drop)
        if self.dropna:
            clean_df = clean_df.dropna()
        for cat in self.to_cat:
            clean_df[cat] = clean_df[cat].cat.codes
            clean_df = clean_df.drop(cat, axis=1)
        if self.to_one_hot:
            clean_df = pd.get_dummies(clean_df, columns=self.to_one_hot)

        clean_df.columns = clean_df.columns.astype(str)
        self.clean_df = clean_df

    def __split_data(self):
        if self.X is None:
            self.X = self.clean_df.drop(self.target, axis=1)
            self.y = self.clean_df[self.target]

        if self.train_eq_test:
            self.X_train = self.X
            self.y_train = self.y
            self.X_test = self.X
            self.y_test = self.y
        else:
            self.X_train, self.X_test, self.y_train, self.y_test = train_test_split(
                self.X, self.y, test_size=self.test_size, random_state=self.random_state)


class Experiment:
    def __init__(self, data_processor, model_class, model_params,
                 shap_class=None, is_tree=True, shap_params=None, dalex_class=None, dalex_params=None,
                 pvi_params=None, pdp_params=None, ale_params=None, pdp_domain=51):
        self.data_processor = data_processor
        self.model_class = model_class
        self.model_params = model_params
        self.shap_class = shap_class
        self.is_tree = is_tree
        self.shap_params = shap_params
        self.dalex_class = dalex_class
        self.dalex_params = dalex_params
        self.pvi_params = pvi_params
        self.pdp_params = pdp_params
        self.ale_params = ale_params
        self.pdp_domain = pdp_domain

        self.times = {}
        self.model = None
        self.shap_exp = None
        self.dx_exp = None

        self.base_metrics = None

        assert self.shap_class is None or self.shap_params is not None, "You can't pass shap_class without shap_params"  # class => params
        assert self.dalex_class is None or self.dalex_params is not None, "You can't pass dalex_class without dalex_params"  # class => params
        assert self.pvi_params is None or self.dalex_class is not None, "You can't pass pvi_params without dalex"
        assert self.pdp_params is None or self.pvi_params is not None, "You can't pass pdp_params without pvi"
        assert self.ale_params is None or self.pvi_params is not None, "You can't pass ale_params without pvi"

        self.__create_train_model()
        self.__calculate_baseline()

    def __create_train_model(self):
        self.model = self.model_class(**self.model_params)
        self.model.fit(self.data_processor.X_train, self.data_processor.y_train)

    def __timeit(self, fun, args=[], kwargs={}, name="", attribute=None):
        st = time.time()
        ret = getattr(fun(*args, **kwargs), attribute) if attribute else fun(*args, **kwargs)
        et = time.time()

        self.times[name] = et - st

        return ret

    @staticmethod
    def kernel_polynomial(y, X, degree=2):
        k_vals = np.sum(X * y, axis=1)
        return (k_vals + 1) ** degree

    @staticmethod
    def kernel_gaussian(y, X, gamma=1):
        k_vals = np.sum((X - y) ** 2, axis=1)
        return np.exp(-gamma * k_vals / 2)

    def __calc_shap(self, data, name):
        if self.shap_class is None:
            return {}


        if self.is_tree:
            shap_exp = self.shap_class(self.model, data=data, **self.shap_params)
        else:
            masker = shap.maskers.Independent(data = self.data_processor.X_train)
            shap_exp = self.shap_class(self.model.predict, masker, **self.shap_params)
        shap_sv = self.__timeit(fun=shap_exp, args=[data], name=name, attribute="values")
        shap_svi = np.absolute(shap_sv).mean(axis=0)

        return {"shap_exp": shap_exp, "shap_sv": shap_sv, "shap_svi": shap_svi}

    def __get_dx(self, X, y):
        return self.dalex_class(self.model, X, y, **self.dalex_params)

    def __calc_pvi(self, dx_exp, X, name):
        if self.pvi_params is None:
            return {}, None, None

        pvi_ = self.__timeit(fun=dx_exp.model_parts, kwargs=self.pvi_params, name=name)
        pvi = pvi_.result.iloc[1:X.shape[1], :].sort_values(
            'variable').dropout_loss  # 1d permutational variable importance
        most_important_variable = pvi_.result[~pvi_.result.variable.isin(['_baseline_', '_full_model_'])].variable.iloc[
            -1]
        variable_splits = {most_important_variable: np.linspace(X[most_important_variable].min(),
                                                                X[most_important_variable].max(),
                                                                num=self.pdp_domain)}

        return {"pvi": pvi}, most_important_variable, variable_splits

    def __calc_pdp(self, dx_exp, most_important_variable, variable_splits, name):
        if self.pdp_params is None:
            return {}

        return {"pdp": self.__calc_pdp_ale(dx_exp, self.pdp_params, most_important_variable, variable_splits, name)}

    def __calc_ale(self, dx_exp, most_important_variable, variable_splits, name):
        if self.ale_params is None:
            return {}

        return {"ale": self.__calc_pdp_ale(dx_exp, self.ale_params, most_important_variable, variable_splits, name)}

    def __calc_pdp_ale(self, dx_exp, params, most_important_variable, variable_splits, name):
        pdp_ale_ = self.__timeit(fun=dx_exp.model_profile,
                                 kwargs=dict(params, **{'variables': most_important_variable,
                                                        'variable_splits': variable_splits}), name=name)
        return pdp_ale_.result[['_yhat_']].to_numpy()

    def __calculate_metrics(self, X, y, name_suffix):
        sample_metrics = {'X': X, 'y': y}
        sample_metrics.update(self.__calc_shap(X, f"sv_{name_suffix}"))

        if self.dalex_class is None:
            return sample_metrics

        dx_exp = self.__get_dx(X, y)
        sample_metrics["dx_exp"] = dx_exp

        pvi, most_important_variable, variable_splits = self.__calc_pvi(dx_exp, X, f"pvi_{name_suffix}")
        sample_metrics.update(pvi)
        sample_metrics.update(self.__calc_pdp(dx_exp, most_important_variable, variable_splits, f"pdp_{name_suffix}"))
        sample_metrics.update(self.__calc_ale(dx_exp, most_important_variable, variable_splits, f"ale_{name_suffix}"))

        return sample_metrics

    def __calculate_baseline(self):
        self.base_metrics = self.__calculate_metrics(self.data_processor.X_test, self.data_processor.y_test, "all")

    @staticmethod
    def compute_wasserstein_distance(X, X_compressed):
        print(X.shape)
        print(X_compressed.shape)
        return np.sum([wasserstein_distance(X[:, i], X_compressed[:, i]) for i in range(X.shape[1])])

    @staticmethod
    def exp_results_to_df(df, base_metrics, random_metrics, compressed_metrics, times, seed, model_metric):
        def calculate_diffs(exp_name, metric_key):
            if metric_key not in base_metrics:

                return {}

            return {f"{exp_name}_random": np.sum(np.abs(base_metrics[metric_key] - random_metrics[metric_key])),
                    f"{exp_name}_compressed": np.sum(np.abs(base_metrics[metric_key] - compressed_metrics[metric_key]))}

        next_row = {}
        print(base_metrics['X'].shape)
        if "dx_exp" in base_metrics:
            print(base_metrics['dx_exp'].model_performance().result)
            next_row.update({'model_performance': base_metrics['dx_exp'].model_performance().result[model_metric].values[0]})

        # metric diffs
        for exp_name, metric_key in [('svi', 'shap_svi'), ('pvi', 'pvi'), ('pdp', 'pdp'), ('ale', 'ale')]:
            next_row.update(calculate_diffs(exp_name, metric_key))

        # time
        next_row.update({f"time_{k}": v for k, v in times.items()})

        # distances
        next_row.update({
            'wd_random': Experiment.compute_wasserstein_distance(base_metrics['X'].to_numpy(),
                                                                 random_metrics['X'].to_numpy()),
            'wd_compressed': Experiment.compute_wasserstein_distance(base_metrics['X'].to_numpy(),
                                                                     compressed_metrics['X'].to_numpy())
        })

        if "shap_sv" in base_metrics:
            next_row.update({'sv_wd_random': Experiment.compute_wasserstein_distance(base_metrics['shap_sv'],
                                                                                     random_metrics['shap_sv']),
                             'sv_wd_compressed': Experiment.compute_wasserstein_distance(base_metrics['shap_sv'],
                                                                                         compressed_metrics['shap_sv'])
                             })

        new_row = pd.DataFrame(next_row, index=[seed])
        df_longer = pd.concat([df, new_row])
        return df_longer

    def run(self, no_tests, kernel, no_halving_rounds=1, compress_oversampling=0,
            save_path=None, model_metric='accuracy'):

        X, y = self.data_processor.X_test, self.data_processor.y_test

        # X = X.reset_index(drop=True)
        # y = y.reset_index(drop=True)
        exp_results = pd.DataFrame()

        for seed in range(no_tests):
            np.random.seed(seed)
            
            f_halve = lambda x: kt.thin(
                X=x,
                m=no_halving_rounds,
                split_kernel=kernel,
                swap_kernel=kernel,
                store_K=True,  # use memory, run faster (bad if you can't fit in memory)
                seed=seed,
                unique=True
            )
            
            ids_compressed = self.__timeit(fun=compress.compress, args=[X.to_numpy()],
                                           kwargs={'halve': f_halve, 'g': compress_oversampling},
                                           name='kt')
            ids_random = self.__timeit(fun=np.random.choice, args=[X.shape[0]],
                                       kwargs={'size': len(ids_compressed), 'replace': False}, name='random')

            X_compressed, y_compressed = X.iloc[ids_compressed], y.iloc[ids_compressed]
            X_random, y_random = X.iloc[ids_random], y.iloc[ids_random]

            compressed_metrics = self.__calculate_metrics(X_compressed, y_compressed, "compressed")
            random_metrics = self.__calculate_metrics(X_random, y_random, "random")

            exp_results = Experiment.exp_results_to_df(
                df=exp_results,
                base_metrics=self.base_metrics,
                random_metrics=random_metrics,
                compressed_metrics=compressed_metrics,
                times=self.times,
                seed=seed,
                model_metric=model_metric
            )


        if save_path is not None:
            exp_results.to_parquet(save_path)

        return exp_results
