# coding=utf-8

from skater.util import exceptions
from rpy2.robjects.packages import importr
from rpy2.robjects import pandas2ri
import numbers
import numpy as np
import pandas as pd
import rpy2.robjects as ro
pandas2ri.activate()


class BRLC(object):
    _estimator_type = "classifier"

    def __init__(self, iterations=30000, pos_sign=1, neg_sign=0, min_rule_len=1,
                 max_rule_len=8, min_support_pos=0.10, min_support_neg=0.10,
                 eta=1.0, n_chains=50, alpha=1, lambda_=10, discretize=True, drop_features=False):
        """
        BRLC(Bayesian Rule List Classifier) is a python wrapper for SBRL(Scalable Bayesian Rule list).
        SBRL is a scalable Bayesian Rule List. It's a generative estimator to build hierarchical interpretable
        decision lists. This python wrapper is an extension to the work done by Professor Cynthia Rudin,
        Benjamin Letham, Hongyu Yang, Margo Seltzer and others. For more information check out the reference section
        below.

        Parameters
        ----------
        iterations: number of iterations for each MCMC chain (default 30000)
        pos_sign: sign for the positive labels in the "label" column.(default "1")
        neg_sign: sign for the negative labels in the "label" column.(default "0")
        min_rule_len: minimum number of cardinality for rules to be mined from the data-frame(default 1)
        max_rule_len: maximum number of cardinality for rules to be mined from the data-frame(default 1)
        min_support_pos: a number between 0 and 1, for the minimum percentage support for the positive
        observations.(default 0.1)
        min_support_neg: a number between 0 and 1, for the minimum percentage support for the negative
        observations.(default 0.1)
        eta:  default 1
        n_chains: default 10
        alpha: a prior pseudo-count for the positive(alpha1) and negative(alpha0) classes. default values (1, 1)
        lambda_: a hyper-parameter for the expected length of the rule list(default 10)
        discretize: apply discretizer to handle continuous features (default True)
        drop_features: once continuous features are discretized, use this flag to drop them (default False)

        References
        ----------
        .. [1] Letham et.al(2015) Interpretable classifiers using rules and Bayesian analysis:
               Building a better stroke prediction model (https://arxiv.org/abs/1511.01644)
        .. [2] Yang et.al(2016) Scalable Bayesian Rule Lists (https://arxiv.org/abs/1602.08610)
        .. [3] https://github.com/Hongyuy/sbrl-python-wrapper/blob/master/sbrl/C_sbrl.py

        """
        self.__r_sbrl = importr('sbrl')
        self.__model = None
        self.__as_factor = ro.r['as.factor']
        self.__s_apply = ro.r['lapply']
        self.__r_frame = ro.r['data.frame']
        self.model_params = {
            "iters": iterations, "pos_sign": pos_sign, "neg_sign": neg_sign, "rule_minlen": min_rule_len,
            "rule_maxlen": max_rule_len, "minsupport_pos": min_support_pos, "minsupport_neg": min_support_neg,
            "eta": eta, "nchain": n_chains, "lambda": lambda_, "alpha": alpha
        }
        self.__discretize = discretize
        self.__drop_features = drop_features
        self.discretized_features = []
        self.feature_names = []


    def set_params(self, params):
        """ Set model hyper-parameters
        """
        self.model_params[list(params.keys())[0]] = list(params.values())[0]


    def discretizer(self, X, column_list, no_of_quantiles=None, labels_for_bin=None, precision=3):
        """ A discretizer for continuous features

        Parameters
        -----------
        X : pandas.DataFrame
            Dataframe containing continuous features
        column_list : list/tuple
        no_of_quantiles : integer/list
            Number of quantiles, e.g. deciles(10), quartiles(4) or as a list of quantiles[0, .25, .5, .75, 1.]
            if 'None' then [0, .25, .5, .75, 1.] is used
        labels_for_bin : labels for the resulting bins
        precision : int
            precision for storing and creating bins

        Returns
        --------
        new_X : pandas.DataFrame
            Contains discretized features

        """
        if not isinstance(X, pd.DataFrame):
            raise TypeError("Only pandas.DataFrame as input type is currently supported")
        q_value = [0, .25, .5, .75, 1.] if no_of_quantiles is None else no_of_quantiles
        q_labels = [1, 2, 3, 4] if labels_for_bin is 'default' else labels_for_bin
        new_X = X.copy()
        for column_name in column_list:
            new_clm_name = '{}_q_label'.format(column_name)
            self.discretized_features.append(new_clm_name)
            new_X.loc[:, new_clm_name] = pd.qcut(X[column_name].rank(method='first'), q=q_value,
                                                 labels=q_labels, duplicates='drop', precision=precision)

            # Drop the continuous feature column which has been discritized
            new_X = new_X.drop([column_name], axis=1) if self.__drop_features else new_X
            # explicitly convert the labels column to 'str' type
            new_X = new_X.astype(dtype={'{}_q_label'.format(column_name): "str"})
        return new_X


    def _filter_continuous_features(self, X, column_list=None):
        import collections
        # Sequence is a base class for list and tuple. column_list could be of either type
        if not isinstance(column_list, collections.Sequence):
            raise TypeError("Only list/tuple type supported for specifying column list")
        c_l = X.columns if column_list is None else column_list
        # To check for numeric type, validate again numbers.Number (base class for numeric type )
        # Reference[PEP-3141]: https://www.python.org/dev/peps/pep-3141/
        numeric_type_columns = tuple(filter(lambda c_name: isinstance(X[c_name].iloc[0], numbers.Number), c_l))
        return numeric_type_columns


    # a helper function to filter unwanted features
    filter_to_be_discretize = lambda self, clmn_list, unwanted_list: \
        tuple(filter(lambda c_name: c_name not in unwanted_list, clmn_list))


    def fit(self, X, y_true, n_quantiles=None, bin_labels='default', undiscretize_feature_list=None, precision=3):
        """ Fit the estimator.

        Parameters
        -----------
            X: pandas.DataFrame object, that could be used by the model for training.
                 It must not have a column named 'label'
            y_true: pandas.Series, 1-D array to store ground truth labels

        Returns
        -------
            SBRL model instance: rpy2.robjects.vectors.ListVector
        """
        if len(np.unique(y_true)) != 2:
            raise Exception("Supports only binary classification right now")

        if not isinstance(X, pd.DataFrame):
            raise exceptions.DataSetError("Only pandas.DataFrame as input type is currently supported")

        # Conditions being managed
        # 1. if 'undiscretize_feature_list' is empty and discretization flag is enabled,
        #    discretize 'all' continuous features
        # 2. if undiscretize_feature_list is not empty and discretization flag is enabled, filter the ones not needed
        #    needed
        for_discretization_clmns = tuple(filter(lambda c_name: c_name not in undiscretize_feature_list, X.columns)) \
            if undiscretize_feature_list is not None else tuple(X.columns)

        data = self.discretizer(X, self._filter_continuous_features(X, for_discretization_clmns),
                                no_of_quantiles=n_quantiles, labels_for_bin=bin_labels, precision=precision) \
            if self.__discretize is True else X

        # record all the feature names
        self.feature_names = data.columns
        data.loc[:, "label"] = y_true
        data_as_r_frame = self.__r_frame(self.__s_apply(data, self.__as_factor))
        self.__model = self.__r_sbrl.sbrl(data_as_r_frame, **self.model_params)
        return self.__model


    def save_model(self, model_name, compress=True):
        """ Persist the model for future use
        """
        import joblib
        if self.__model is not None:
            joblib.dump(self.__model, model_name, compress=compress)
        else:
            raise Exception("SBRL model is not fitted yet; no relevant model instance present")


    def load_model(self, serialized_model_name):
        """ Load a serialized model
        """
        import joblib
        try:
            self.__model = joblib.load(serialized_model_name)
        except (OSError, IOError) as err:
            print("Something is not right with the serialization format. Details {}".format(err))
            raise


    def predict_proba(self, X):
        """
        Parameters
        -----------
            X :  pandas.DataFrame object, representing the data to be making predictions on.
            `type`  whether the prediction is discrete or probabilistic.

        Returns
        -------
            return a numpy.ndarray of shape (#datapoints, 2), the probability for each observations
        """
        if not isinstance(X, pd.DataFrame):
            raise exceptions.DataSetError("Only pandas.DataFrame as input type is currently supported")

        data_as_r_frame = self.__r_frame(self.__s_apply(X, self.__as_factor))
        results = self.__r_sbrl.predict_sbrl(self.__model, data_as_r_frame)
        return pandas2ri.ri2py_dataframe(results).T


    def predict(self, X=None, prob_score=None, threshold=0.5, pos_label=1):
        """ Predict the class for input data
        The predicted class is determined by setting a threshold on the Adjust threshold to
        balance between sensitivity and specificity

        """
        # TODO: Extend it for multi-class classification
        probability_df = self.predict_proba(X) if X is not None and prob_score is None else prob_score
        y_prob = probability_df.loc[:, pos_label]
        y_prob['label'] = np.where(y_prob.values > threshold, 1, 0)
        return y_prob, y_prob['label']


    def print_model(self):
        """ print the decision stumps of the learned estimator
        """
        self.__r_sbrl.print_sbrl(self.__model)


    def access_learned_rules(self, rule_indexes):
        """ Helper function to access all learned decision rules
        """
        if not isinstance(rule_indexes, str):
            raise TypeError('Expected type string {} provided'.format(type(rule_indexes)))

        # Convert model properties into a readable python dict
        result_dict = dict(zip(self.__model.names, map(list, list(self.__model))))

        indexes_func = lambda indexes: [int(v) for v in indexes.split(':')]
        # original index starts from 0 while the printed index starts from 1, hence adjust the index
        rules_filter = lambda all_rules, indexes: all_rules['rulenames'][(indexes[0] - 1):(indexes[1] - 1)] \
            if rule_indexes.find(':') > -1 else all_rules['rulenames'][indexes[0] - 1]

        # Enable the ability to access single or multiple sequential model learned decisions
        rules_result = result_dict['rulenames'] if rule_indexes == "all" \
            else rules_filter(result_dict, indexes_func(rule_indexes))
        return rules_result
